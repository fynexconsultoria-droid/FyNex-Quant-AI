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

    def get_live_champion_meta_path(self, symbol: str, timeframe: str) -> str:
        clean_symbol = self._sanitize_token(symbol, "symbol")
        clean_tf = self._sanitize_token(timeframe, "timeframe")
        return self._safe_model_path(f"live_champion_{clean_symbol}_{clean_tf}.json")

    def get_live_champion_model_path(self, symbol: str, timeframe: str) -> str:
        clean_symbol = self._sanitize_token(symbol, "symbol")
        clean_tf = self._sanitize_token(timeframe, "timeframe")
        return self._safe_model_path(f"live_champion_{clean_symbol}_{clean_tf}.joblib")

    @staticmethod
    def _atomic_dump_joblib(obj: Any, target_path: str) -> None:
        directory = os.path.dirname(os.path.abspath(target_path)) or "."
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".model_", suffix=".tmp")
        os.close(fd)
        try:
            joblib.dump(obj, tmp_path, compress=3)
            try:
                with open(tmp_path, "rb") as f:
                    os.fsync(f.fileno())
            except OSError:
                pass
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
            except OSError as e:
                logger.warning(f"Falha ao arquivar modelo '{fpath}': {e}")
        # Também remove/arquiva ponteiros latest e metadata incompatíveis
        for extra_p in (
            self._safe_model_path(f"latest_{clean_symbol}_{clean_tf}.joblib"),
            self.get_meta_path(symbol, timeframe),
        ):
            if os.path.exists(extra_p):
                try:
                    shutil.move(extra_p, os.path.join(archive_dir, os.path.basename(extra_p)))
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
        pinned_versions: Optional[List[int]] = None,
    ) -> None:
        if self.max_versions_kept <= 0:
            return
        pinned = set(int(v) for v in (pinned_versions or []) if v is not None)
        if active_version is not None:
            pinned.add(int(active_version))
        versioned = self._list_versioned_files(clean_symbol, timeframe)
        versioned.sort(key=lambda x: (x[1], x[0]), reverse=True)
        kept: List[Tuple[int, float, str]] = []
        to_remove: List[Tuple[int, float, str]] = []
        for item in versioned:
            ver, _, _ = item
            if ver in pinned:
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

        # 1) Grava arquivo versionado primeiro -> 2) atualiza metadata apontando para ele -> 3) atualiza latest
        # Evita leituras rasgadas (torn reads) onde latest já mudou mas metadata ainda é da versão antiga.
        self._atomic_dump_joblib(model, version_path)
        live_champ_meta = self.load_live_champion_metadata(symbol, timeframe)
        pinned_vers = [int(live_champ_meta["version"])] if live_champ_meta and live_champ_meta.get("version") else []
        self._prune_old_versions(clean_symbol, clean_tf, active_version=int(version), pinned_versions=pinned_vers)

        metadata: Dict[str, Any] = dict(extra_meta) if isinstance(extra_meta, dict) else {}
        metadata.update({
            "version": version,
            "accuracy": float(accuracy),
            "feature_cols": feature_cols,
            "feature_schema_hash": self.feature_schema_hash(feature_cols),
            "symbol": symbol,
            "timeframe": timeframe,
            "saved_at": utc_now_str(),
            "file": version_filename,
        })

        pair_meta_path = self.get_meta_path(symbol, timeframe)
        self._atomic_dump_json(metadata, pair_meta_path)
        # Atualiza metadata.json global apenas para BTC/USDT (ou se ainda não existir), evitando sobrescrita por treinos Forex em lote
        if symbol == "BTC/USDT" or not os.path.exists(self.meta_path):
            self._atomic_dump_json(metadata, self.meta_path)
        self._atomic_dump_joblib(model, latest_path)

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
            load_target = latest_path
            referenced_file = meta.get("file")
            if referenced_file:
                try:
                    ref_path = self._safe_model_path(str(referenced_file))
                except ValueError:
                    logger.warning(f"Caminho inválido referenciado no metadata ('{referenced_file}'); ignorando.")
                    ref_path = ""
                if ref_path and os.path.exists(ref_path):
                    load_target = ref_path
                elif ref_path:
                    logger.warning(f"Arquivo referenciado no metadata ('{referenced_file}') não existe; usando latest.")
            features = list(meta.get("feature_cols", []))
            expected_hash = meta.get("feature_schema_hash")
            if expected_hash and features and self.feature_schema_hash(features) != expected_hash:
                logger.warning(f"Hash de schema divergente no metadata de {symbol} {timeframe}; ignorando modelo.")
                return None
            model = joblib.load(load_target)
            version = int(meta.get("version", 1))
            acc = float(meta.get("accuracy", 0.5))

            logger.info(
                f"🧠 [MODELO RECUPERADO DE DISCO] Carregado Modelo v{version} "
                f"({symbol} {timeframe}) (Acurácia: {acc * 100:.2f}%)"
            )
            return model, version, acc, features
        except Exception as e:
            logger.warning(f"Não foi possível restaurar modelo salvo: {e}. Um novo modelo será treinado.")
            return None

    def list_available_versions(self, symbol: str, timeframe: str) -> List[Dict[str, Any]]:
        """Lista versões de modelos salvas em disco para (symbol, timeframe), da mais recente para a mais antiga."""
        clean_symbol = self._sanitize_token(symbol, "symbol")
        clean_tf = self._sanitize_token(timeframe, "timeframe")
        latest_meta = self.load_metadata(symbol, timeframe)
        live_meta = self.load_live_champion_metadata(symbol, timeframe)
        latest_ver = int(latest_meta.get("version", 0)) if latest_meta else 0
        live_ver = int(live_meta.get("version", 0)) if live_meta else 0

        items: List[Dict[str, Any]] = []
        versioned = self._list_versioned_files(clean_symbol, clean_tf)
        versioned.sort(key=lambda x: x[0], reverse=True)
        for ver, mtime, fpath in versioned:
            items.append({
                "version": int(ver),
                "file": os.path.basename(fpath),
                "is_demo_latest": int(ver) == latest_ver,
                "is_live_champion": int(ver) == live_ver,
                "modified_ts": mtime,
            })
        return items

    def load_live_champion_metadata(self, symbol: str, timeframe: str) -> Optional[Dict[str, Any]]:
        """Lê os metadados do modelo Campeão promovido especificamente para a Conta Real (Live)."""
        champ_meta_path = self.get_live_champion_meta_path(symbol, timeframe)
        if not os.path.exists(champ_meta_path):
            return None
        try:
            with open(champ_meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            if meta.get("symbol") == symbol and meta.get("timeframe") == timeframe:
                return meta
        except Exception:
            return None
        return None

    def promote_to_live(
        self,
        symbol: str,
        timeframe: str,
        version: Optional[int] = None,
        promoted_by: str = "manual",
        notes: str = "",
    ) -> Dict[str, Any]:
        """
        Promove um modelo treinado na Conta Demo/Treino para ser o Campeão oficial da Conta Real (Live).
        Copia atomicamente o artefato `.joblib` para `live_champion_{sym}_{tf}.joblib` e grava o manifesto JSON.
        """
        clean_symbol = self._sanitize_token(symbol, "symbol")
        clean_tf = self._sanitize_token(timeframe, "timeframe")
        latest_meta = self.load_metadata(symbol, timeframe) or {}

        target_ver = int(version) if version is not None else int(latest_meta.get("version", 0) or 0)
        if target_ver <= 0:
            versioned = self._list_versioned_files(clean_symbol, clean_tf)
            if not versioned:
                raise FileNotFoundError(
                    f"Nenhum modelo treinado encontrado para {symbol} ({timeframe}). Treine a IA na Conta Demo primeiro."
                )
            versioned.sort(key=lambda x: x[0], reverse=True)
            target_ver = int(versioned[0][0])

        src_filename = f"model_v{target_ver}_{clean_symbol}_{clean_tf}.joblib"
        src_path = self._safe_model_path(src_filename)
        if not os.path.exists(src_path):
            latest_path = self._safe_model_path(f"latest_{clean_symbol}_{clean_tf}.joblib")
            if os.path.exists(latest_path) and int(latest_meta.get("version", 0) or 0) == target_ver:
                src_path = latest_path
            else:
                raise FileNotFoundError(
                    f"Arquivo do modelo v{target_ver} ({src_filename}) não encontrado em '{self.models_dir}'."
                )

        # Valida integridade carregando o modelo antes de promover para dinheiro real
        model = joblib.load(src_path)
        classes = list(getattr(model, "classes_", []))
        if len(classes) < 2:
            raise ValueError(
                f"Modelo v{target_ver} rejeitado para Conta Real: modelo degenerado (classes={classes})."
            )

        val_metrics = getattr(model, "validation_metrics_", None)
        if not isinstance(val_metrics, dict):
            val_metrics = latest_meta.get("validation") if isinstance(latest_meta.get("validation"), dict) else {}

        feature_cols = list(latest_meta.get("feature_cols", []))
        accuracy = float(
            val_metrics.get("precision_buy")
            or val_metrics.get("accuracy")
            or latest_meta.get("accuracy", 0.5)
        )

        champ_model_path = self.get_live_champion_model_path(symbol, timeframe)
        champ_meta_path = self.get_live_champion_meta_path(symbol, timeframe)

        # Grava o modelo campeão primeiro, depois o manifesto JSON (evita torn reads)
        self._atomic_dump_joblib(model, champ_model_path)

        champ_meta: Dict[str, Any] = {
            "symbol": symbol,
            "timeframe": timeframe,
            "version": target_ver,
            "accuracy": accuracy,
            "feature_cols": feature_cols,
            "feature_schema_hash": self.feature_schema_hash(feature_cols) if feature_cols else latest_meta.get("feature_schema_hash"),
            "file": os.path.basename(champ_model_path),
            "source_file": src_filename,
            "model_type": str(val_metrics.get("model_type", latest_meta.get("model_type", "ensemble"))),
            "validation": val_metrics,
            "promoted_at": utc_now_str(),
            "promoted_by": str(promoted_by or "manual"),
            "notes": str(notes or ""),
            "saved_at": latest_meta.get("saved_at") or utc_now_str(),
        }
        self._atomic_dump_json(champ_meta, champ_meta_path)
        logger.info(
            f"🏆 [PROMOÇÃO CONTA REAL] Modelo v{target_ver} ({symbol} {timeframe}) promovido a Campeão Live "
            f"(Score: {accuracy * 100:.2f}%, por: {promoted_by})."
        )
        return champ_meta

    def load_live_champion_model(
        self,
        symbol: str,
        timeframe: str,
        allow_fallback_to_latest: bool = True,
    ) -> Optional[Tuple[RandomForestClassifier, int, float, List[str]]]:
        """
        Carrega o modelo Campeão explicitamente promovido para a Conta Real.
        Se ainda não houver promoção explícita e `allow_fallback_to_latest=True`,
        faz fallback seguro para o modelo mais recente validado em disco.
        """
        meta = self.load_live_champion_metadata(symbol, timeframe)
        champ_model_path = self.get_live_champion_model_path(symbol, timeframe)
        if meta and os.path.exists(champ_model_path):
            try:
                features = list(meta.get("feature_cols", []))
                expected_hash = meta.get("feature_schema_hash")
                if expected_hash and features and self.feature_schema_hash(features) != expected_hash:
                    logger.warning(f"Hash divergente no Campeão Live de {symbol} {timeframe}; ignorando.")
                    return None
                model = joblib.load(champ_model_path)
                version = int(meta.get("version", 1))
                acc = float(meta.get("accuracy", 0.5))
                logger.info(
                    f"💎 [CAMPEÃO LIVE CARREGADO] Modelo v{version} ({symbol} {timeframe}) "
                    f"(Acurácia: {acc * 100:.2f}%, promovido em {meta.get('promoted_at')})"
                )
                return model, version, acc, features
            except Exception as e:
                logger.warning(f"Falha ao carregar Campeão Live ({symbol} {timeframe}): {e}")
                if not allow_fallback_to_latest:
                    return None
        if allow_fallback_to_latest:
            return self.load_latest_model(symbol, timeframe)
        return None
