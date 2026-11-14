"""Checkpoint contract shared by the controller and the C3/C4 workloads.

  <name>/data-<ts>.json   the saved state (uploaded FIRST)
  <name>/latest.json      manifest {"ts":..} (written LAST => a checkpoint only counts once complete)
  requests/<name>.json    {"ts":..} written by Lifeboat to ask the app to save now
  events/<name>/<ts>-<kind>.json   {"name","kind","unit","ts"} lifecycle event (start|term|done),
                          written by the worker for E3 (pod logs vanish when a Job is suspended)
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional


class CheckpointStore:
    def _put(self, key: str, data: bytes) -> None: raise NotImplementedError
    def _get(self, key: str) -> Optional[bytes]: raise NotImplementedError
    def list_keys(self, prefix: str) -> list[str]: raise NotImplementedError

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

    # ---- E3 lifecycle events (app side writes, e3_collect reads)
    def record_event(self, name: str, kind: str, unit: int) -> dict:
        ts = time.time()
        ev = {"name": name, "kind": kind, "unit": int(unit), "ts": ts}
        self._put(f"events/{name}/{ts:.3f}-{kind}.json", json.dumps(ev).encode())
        return ev

    def events(self) -> list[dict]:
        out = []
        for key in self.list_keys("events/"):
            raw = self._get(key)
            if raw:
                out.append(json.loads(raw))
        return sorted(out, key=lambda e: e["ts"])

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

    def list_keys(self, prefix: str) -> list[str]:
        out = []
        for dirpath, _dirs, files in os.walk(self.root):
            for f in files:
                if f.endswith(".tmp"):
                    continue
                key = os.path.relpath(os.path.join(dirpath, f), self.root).replace(os.sep, "/")
                if key.startswith(prefix):
                    out.append(key)
        return sorted(out)


class S3CheckpointStore(CheckpointStore):
    def __init__(self, bucket: str, endpoint: str = ""):
        import boto3                                # lazy: tests don't need it
        from botocore.config import Config as BotoConfig
        self.bucket = bucket
        # short timeouts: the controller reads checkpoints every tick, and boto3's default 60 s
        # would let a slow S3/MinIO stall the whole loop (including C1 scale-up)
        self.s3 = boto3.client("s3", endpoint_url=endpoint or None, config=BotoConfig(
            connect_timeout=2, read_timeout=3, retries={"max_attempts": 2}))
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        """Create the bucket if missing (fresh MinIO). Not fatal: S3 may just be slow to start."""
        try:
            self.s3.head_bucket(Bucket=self.bucket)
        except Exception:
            try:
                self.s3.create_bucket(Bucket=self.bucket)
            except Exception as e:
                logging.getLogger("lifeboat.checkpoint").warning("bucket %s not ready: %s", self.bucket, e)

    def _put(self, key: str, data: bytes) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data)

    def _get(self, key: str) -> Optional[bytes]:
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except self.s3.exceptions.NoSuchKey:
            return None

    def list_keys(self, prefix: str) -> list[str]:
        out = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            out.extend(o["Key"] for o in page.get("Contents", []))
        return sorted(out)


def from_env() -> CheckpointStore:
    """Used by both the controller and the worker image (LIFEBOAT_* env vars).
    In a cluster this must be s3 (MinIO or AWS): with "file" the controller and each worker
    have their own /tmp, so Lifeboat never sees an app's checkpoint."""
    if os.getenv("LIFEBOAT_CHECKPOINT_BACKEND", "file") == "s3":
        return S3CheckpointStore(os.getenv("LIFEBOAT_S3_BUCKET", "lifeboat-checkpoints"),
                                 os.getenv("LIFEBOAT_S3_ENDPOINT", ""))
    return FileCheckpointStore(os.getenv("LIFEBOAT_CHECKPOINT_DIR", "/tmp/lifeboat-checkpoints"))
