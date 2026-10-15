"""Checkpoint contract shared by the controller and the C3/C4 workloads.

  <name>/data-<ts>.json   the saved state (uploaded FIRST)
  <name>/latest.json      manifest {"ts":..} (written LAST => a checkpoint only counts once complete)
  requests/<name>.json    {"ts":..} written by Lifeboat to ask the app to save now
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional


class CheckpointStore:
    def _put(self, key: str, data: bytes) -> None: raise NotImplementedError
    def _get(self, key: str) -> Optional[bytes]: raise NotImplementedError

    # ---- app side
    def save(self, name: str, state: dict) -> float:
        ts = time.time()
        self._put(f"{name}/data-{ts:.3f}.json", json.dumps(state).encode())
        self._put(f"{name}/latest.json", json.dumps({"ts": ts, "key": f"{name}/data-{ts:.3f}.json"}).encode())
        return ts

    def load(self, name: str) -> Optional[dict]:
        m = self._get(f"{name}/latest.json")
        if not m:
            return None
        data = self._get(json.loads(m)["key"])
        return json.loads(data) if data else None

    def last_request_ts(self, name: str) -> float:
        raw = self._get(f"requests/{name}.json")
        return json.loads(raw)["ts"] if raw else 0.0

    # ---- Lifeboat side
    def latest_ts(self, name: str) -> Optional[float]:
        m = self._get(f"{name}/latest.json")
        return json.loads(m)["ts"] if m else None

    def request(self, name: str) -> float:
        ts = time.time()
        self._put(f"requests/{name}.json", json.dumps({"ts": ts}).encode())
        return ts


class FileCheckpointStore(CheckpointStore):
    def __init__(self, root: str):
        self.root = root

    def _put(self, key: str, data: bytes) -> None:
        path = os.path.join(self.root, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)                      # atomic

    def _get(self, key: str) -> Optional[bytes]:
        try:
            with open(os.path.join(self.root, key), "rb") as f:
                return f.read()
        except FileNotFoundError:
            return None


class S3CheckpointStore(CheckpointStore):
    def __init__(self, bucket: str, endpoint: str = ""):
        import boto3                                # lazy: tests don't need it
        self.bucket = bucket
        self.s3 = boto3.client("s3", endpoint_url=endpoint or None)

    def _put(self, key: str, data: bytes) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data)

    def _get(self, key: str) -> Optional[bytes]:
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except self.s3.exceptions.NoSuchKey:
            return None


def from_env() -> CheckpointStore:
    """Used by both the controller and the worker image (LIFEBOAT_* env vars)."""
    if os.getenv("LIFEBOAT_CHECKPOINT_BACKEND", "file") == "s3":
        return S3CheckpointStore(os.getenv("LIFEBOAT_S3_BUCKET", "lifeboat-checkpoints"),
                                 os.getenv("LIFEBOAT_S3_ENDPOINT", ""))
    return FileCheckpointStore(os.getenv("LIFEBOAT_CHECKPOINT_DIR", "/tmp/lifeboat-checkpoints"))
