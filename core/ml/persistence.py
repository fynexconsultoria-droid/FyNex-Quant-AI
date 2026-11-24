"""
Persistência Atômica e Versionamento de Modelos ML (FyNex Quant AI)
===================================================================
Salva e recupera modelos `.joblib` com metadados por (symbol, timeframe),
gravação atômica (.tmp + os.replace), validação de path traversal,
arquivamento de schemas incompatíveis e poda controlada de versões antigas.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from typing import Any, Dict, List, Optional, Tuple

import joblib
from sklearn.ensemble import RandomForestClassifier

from core.infra.utils import sanitize_token, utc_now_str
from core.logging_setup import DEFAULT_MODELS_DIR, logger


class ModelPersistence:
    """
    Gerencia o salvamento e carregamento dos modelos treinados em disco,
    mantendo metadados por (symbol, timeframe), gravação atômica (.tmp + os.replace)
    e poda controlada de versões antigas.
    """

    def __init__(self, models_dir: str = DEFAULT_MODELS_DIR, max_versions_kept: int = 5):
        self.models_dir = models_dir
        self.max_versions_kept = max_versions_kept
        os.makedirs(self.models_dir, exist_ok=True)
        self.meta_path = os.path.join(self.models_dir, "metadata.json")

    @staticmethod
    def _sanitize_token(value: str, label: str) -> str:
        return sanitize_token(value, label=label)

    def _safe_model_path(self, filename: str) -> str:
        base_real = os.path.realpath(self.models_dir)
        target_real = os.path.realpath(os.path.join(base_real, filename))
        if os.path.commonpath([base_real, target_real]) != base_real:
            raise ValueError(f"Caminho fora do diretório de modelos rejeitado: {filename!r}")
        return target_real

    def get_meta_path(self, symbol: str, timeframe: str) -> str:
        clean_symbol = self._sanitize_token(symbol, "symbol")
        clean_tf = self._sanitize_token(timeframe, "timeframe")
        return self._safe_model_path(f"metadata_{clean_symbol}_{clean_tf}.json")

    @staticmethod
    def _atomic_dump_joblib(obj: Any, target_path: str) -> None:
        directory = os.path.dirname(os.path.abspath(target_path)) or "."
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".model_", suffix=".tmp")
        os.close(fd)
        try:
            joblib.dump(obj, tmp_path, compress=3)
            os.replace(tmp_path, target_path)
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise

    @staticmethod
    def _atomic_dump_json(data: Dict[str, Any], target_path: str) -> None:
        directory = os.path.dirname(os.path.abspath(target_path)) or "."
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".meta_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, target_path)
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise

    @staticmethod
    def feature_schema_hash(feature_cols: List[str]) -> str:
        raw = json.dumps(list(feature_cols), separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:16]

    def _list_versioned_files(self, clean_symbol: str, timeframe: str) -> List[Tuple[int, float, str]]:
        prefix = "model_v"
        suffix = f"_{clean_symbol}_{timeframe}.joblib"
        versioned: List[Tuple[int, float, str]] = []
        if not os.path.isdir(self.models_dir):
            return versioned
        for fname in os.listdir(self.models_dir):
            if fname.startswith(prefix) and fname.endswith(suffix):
                ver_str = fname[len(prefix):-len(suffix)]
                if ver_str.isdigit():
                    fpath = os.path.join(self.models_dir, fname)
                    try:
                        mtime = os.path.getmtime(fpath)
                    except OSError:
                        mtime = 0.0
                    versioned.append((int(ver_str), mtime, fpath))
        return versioned

    def next_version(self, symbol: str, timeframe: str) -> int:
        """Retorna a próxima versão monotonicamente crescente (nunca volta para v1)."""
        clean_symbol = self._sanitize_token(symbol, "symbol")
        clean_tf = self._sanitize_token(timeframe, "timeframe")
        disk_versions = [v for v, _, _ in self._list_versioned_files(clean_symbol, clean_tf)]
        meta = self.load_metadata(symbol, timeframe)
        meta_ver = int(meta.get("version", 0)) if meta else 0
        highest = max(disk_versions + [meta_ver, 0])
        return highest + 1

    def archive_incompatible_models(self, symbol: str, timeframe: str) -> int:
        """Move modelos antigos/incompatíveis do par/timeframe para `models/archive/`."""
        clean_symbol = self._sanitize_token(symbol, "symbol")
        clean_tf = self._sanitize_token(timeframe, "timeframe")
        archive_dir = self._safe_model_path("archive")
        os.makedirs(archive_dir, exist_ok=True)
        moved = 0
        for _, _, fpath in self._list_versioned_files(clean_symbol, clean_tf):
            dst = os.path.join(archive_dir, os.path.basename(fpath))
            try:
                shutil.move(fpath, dst)
                moved += 1
            except OSError:
                pass
        if moved > 0:
            logger.info(f"📦 [ARQUIVO] {moved} modelo(s) incompatível(is) movido(s) para '{archive_dir}'.")
        return moved

    def _prune_old_versions(
        self,
        clean_symbol: str,
        timeframe: str,
        active_version: Optional[int] = None,
    ) -> None:
        if self.max_versions_kept <= 0:
            return
        versioned = self._list_versioned_files(clean_symbol, timeframe)
        versioned.sort(key=lambda x: (x[1], x[0]), reverse=True)
        kept: List[Tuple[int, float, str]] = []
        to_remove: List[Tuple[int, float, str]] = []
        for item in versioned:
            ver, _, _ = item
            if active_version is not None and ver == active_version:
                kept.append(item)
            elif len(kept) < self.max_versions_kept:
                kept.append(item)
            else:
                to_remove.append(item)
        for _, _, old_path in to_remove:
            try:
                os.remove(old_path)
            except OSError:
                pass

    def save_model(
        self,
        model: RandomForestClassifier,
        version: int,
        accuracy: float,
        feature_cols: List[str],
        symbol: str,
        timeframe: str,
        extra_meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        clean_symbol = self._sanitize_token(symbol, "symbol")
        clean_tf = self._sanitize_token(timeframe, "timeframe")
        version_filename = f"model_v{int(version)}_{clean_symbol}_{clean_tf}.joblib"
        version_path = self._safe_model_path(version_filename)
        latest_path = self._safe_model_path(f"latest_{clean_symbol}_{clean_tf}.joblib")

        self._atomic_dump_joblib(model, version_path)
        self._atomic_dump_joblib(model, latest_path)
        self._prune_old_versions(clean_symbol, clean_tf, active_version=int(version))

        metadata: Dict[str, Any] = {
            "version": version,
            "accuracy": float(accuracy),
            "feature_cols": feature_cols,
            "feature_schema_hash": self.feature_schema_hash(feature_cols),
            "symbol": symbol,
            "timeframe": timeframe,
            "saved_at": utc_now_str(),
            "file": version_filename,
        }
        if extra_meta:
            metadata.update(extra_meta)

        pair_meta_path = self.get_meta_path(symbol, timeframe)
        self._atomic_dump_json(metadata, pair_meta_path)
        self._atomic_dump_json(metadata, self.meta_path)

        logger.info(
            f"💾 [MODELO SALVO EM DISCO] Versão v{version} gravada em '{version_path}' (Score: {accuracy * 100:.2f}%)"
        )
        return version_path

    def load_metadata(self, symbol: str, timeframe: str) -> Optional[Dict[str, Any]]:
        pair_meta_path = self.get_meta_path(symbol, timeframe)
        for path in (pair_meta_path, self.meta_path):
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    if meta.get("symbol") == symbol and meta.get("timeframe") == timeframe:
                        return meta
                except Exception:
                    continue
        return None

    def load_latest_model(
        self,
        symbol: str,
        timeframe: str,
    ) -> Optional[Tuple[RandomForestClassifier, int, float, List[str]]]:
        clean_symbol = self._sanitize_token(symbol, "symbol")
        clean_tf = self._sanitize_token(timeframe, "timeframe")
        latest_path = self._safe_model_path(f"latest_{clean_symbol}_{clean_tf}.joblib")

        if not os.path.exists(latest_path):
            return None

        meta = self.load_metadata(symbol, timeframe)
        if not meta:
            return None

        try:
            referenced_file = meta.get("file")
            if referenced_file:
                try:
                    ref_path = self._safe_model_path(str(referenced_file))
                except ValueError:
                    logger.warning(f"Caminho inválido referenciado no metadata ('{referenced_file}'); ignorando.")
                    ref_path = ""
                if ref_path and not os.path.exists(ref_path):
                    logger.warning(f"Arquivo referenciado no metadata ('{referenced_file}') não existe; usando latest.")
            model = joblib.load(latest_path)
            version = int(meta.get("version", 1))
            acc = float(meta.get("accuracy", 0.5))
            features = meta.get("feature_cols", [])

            logger.info(
                f"🧠 [MODELO RECUPERADO DE DISCO] Carregado Modelo v{version} "
                f"({symbol} {timeframe}) (Acurácia: {acc * 100:.2f}%)"
            )
            return model, version, acc, features
        except Exception as e:
            logger.warning(f"Não foi possível restaurar modelo salvo: {e}. Um novo modelo será treinado.")
            return None
