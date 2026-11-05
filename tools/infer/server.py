#!/usr/bin/env python3
"""Continuous RM65 pi0.5 inference service. No robot connection or actuation."""
from __future__ import annotations

import argparse
import asyncio
import hmac
import http
import json
import logging
import math
import os
from pathlib import Path
import socket
import sys
import time
import uuid

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "infer"

from .protocol import (ACTION_NAMES, ACTION_TIMING, MAX_MESSAGE_BYTES, PROTOCOL,
                       decode_request, dumps, loads, validate_actions)


class PolicyServer:
    def __init__(self, policy, metadata, *, inference_timeout_s=30., token=None):
        self.policy = policy
        self.metadata = {**metadata, "protocol": PROTOCOL, "action_names": ACTION_NAMES,
                         "action_timing": ACTION_TIMING,
                         "state_dim": 7, "image_shape": [224, 224, 3], "single_controller": True}
        self.timeout = inference_timeout_s
        self.token = token
        self.active_client = None
        self.busy = False
        self.fault = None
        self.completed = 0
        self._model_tasks = set()

    def process_request(self, connection, request):
        if self.token and not hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + self.token):
            return connection.respond(http.HTTPStatus.UNAUTHORIZED, "Unauthorized\n")
        if request.path == "/healthz":
            code = http.HTTPStatus.SERVICE_UNAVAILABLE if self.fault else http.HTTPStatus.OK
            return connection.respond(code, dumps({"ready": not bool(self.fault), "busy": self.busy,
                "connected": self.active_client is not None, "completed_requests": self.completed,
                "fault": self.fault, **self.metadata}) + "\n")
        return None

    async def _error(self, socket, session, request_id, code, message):
        await socket.send(dumps({"type": "error", "protocol": PROTOCOL, "session_id": session,
                                 "request_id": request_id, "code": code, "message": message}))

    async def _infer(self, socket, session, request_id, observation):
        import websockets

        started = time.monotonic()
        task = asyncio.create_task(asyncio.to_thread(self.policy.infer, observation))
        self._model_tasks.add(task)
        task.add_done_callback(self._model_tasks.discard)
        try:
            prediction = await asyncio.wait_for(asyncio.shield(task), timeout=self.timeout)
            actions = validate_actions(prediction["actions"], self.metadata["action_horizon"])
            elapsed_ms = (time.monotonic() - started) * 1000
            reply = {"type": "result", "protocol": PROTOCOL, "session_id": session,
                "request_id": request_id, "actions": actions.tolist(), "sample_rate_hz": 10,
                "action_timing": ACTION_TIMING,
                "server_timing": {"inference_ms": elapsed_ms},
                "gripper_out_of_range_count": int(((actions[:, 6] < 0) | (actions[:, 6] > 1)).sum())}
            await socket.send(dumps(reply))
            self.completed += 1
            logging.info("session=%s request=%s inference_ms=%.1f shape=%s", session, request_id, elapsed_ms, actions.shape)
        except websockets.ConnectionClosed:
            logging.info("连接已断开，丢弃 request=%s 的预测", request_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # GPU/模型失败闭锁服务，不启动第二个调用或自动重新加载模型。
            self.fault = (f"InferenceTimeout: exceeded {self.timeout:g}s; restart service"
                          if isinstance(exc, asyncio.TimeoutError) else f"{type(exc).__name__}: {exc}")
            logging.exception("推理服务进入故障状态，需重启")
            try:
                await self._error(socket, session, request_id, "inference_failed", self.fault)
                await socket.close(code=1011, reason="Inference failed; restart service")
            except websockets.ConnectionClosed:
                pass
        finally:
            self.busy = False

    async def handler(self, socket):
        import websockets

        session = uuid.uuid4().hex
        if self.active_client is not None or self.fault:
            await self._error(socket, session, None, "unavailable", self.fault or "另一个控制客户端已连接")
            await socket.close(code=1013, reason="Controller unavailable")
            return
        self.active_client = session
        job = None
        last_request_id = 0
        try:
            await socket.send(dumps({"type": "hello", "session_id": session, **self.metadata}))
            logging.info("客户端连接 session=%s peer=%s", session, socket.remote_address)
            async for message in socket:
                request_id = None
                try:
                    raw = loads(message)
                    request_id = raw.get("request_id")
                    request_id, observation = decode_request(message, self.metadata.get("default_prompt"))
                    if self.fault:
                        await self._error(socket, session, request_id, "unavailable", self.fault)
                        await socket.close(code=1011, reason="Service faulted")
                        break
                    if self.busy:
                        await self._error(socket, session, request_id, "busy", "上一条推理尚未完成；不排队新请求")
                        continue
                    if request_id <= last_request_id:
                        raise ValueError("同一连接的request_id必须严格递增，不能重放旧请求")
                    last_request_id = request_id
                    self.busy = True
                    job = asyncio.create_task(self._infer(socket, session, request_id, observation))
                except (ValueError, TypeError, KeyError) as exc:
                    await self._error(socket, session, request_id if type(request_id) is int else None,
                                      "invalid_request", str(exc))
        except websockets.ConnectionClosed:
            pass
        finally:
            # 断连后不将模型结果交给下一个会话；当前调用结束前不接第二个客户端。
            if job is not None:
                await asyncio.shield(job)
            if self.active_client == session:
                self.active_client = None
            logging.info("客户端断开 session=%s", session)

    async def run(self, host, port):
        from websockets.asyncio.server import serve

        async with serve(self.handler, host, port, process_request=self.process_request,
                         compression=None, max_size=MAX_MESSAGE_BYTES, max_queue=1,
                         ping_interval=20, ping_timeout=20, close_timeout=3) as listener:
            if not listener.sockets:
                raise RuntimeError("监听端口未成功建立，请检查网络/运行权限")
            logging.info("READY ws://%s:%s，模型已加载并预热；健康检查 /healthz", host, port)
            await asyncio.Future()


def settings_from_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--openpi-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--checkpoint")
    parser.add_argument("--asset-id")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--gpu")
    parser.add_argument("--num-steps", type=int)
    parser.add_argument("--check-only", action="store_true", help="只检查配置和checkpoint目录，不导入JAX或启动服务")
    args = parser.parse_args(argv)
    settings = json.loads(args.config.read_text(encoding="utf-8"))
    expected = {"checkpoint", "asset_id", "host", "port", "gpu", "num_steps", "default_prompt", "inference_timeout_s"}
    if not isinstance(settings, dict) or set(settings) != expected:
        raise ValueError(f"配置字段应恰好为{sorted(expected)}")
    for name in ("checkpoint", "asset_id", "host", "port", "gpu", "num_steps"):
        value = getattr(args, name)
        if value is not None:
            settings[name] = value
    for name in ("checkpoint", "host", "gpu", "default_prompt"):
        if not isinstance(settings[name], str) or not settings[name].strip():
            raise ValueError(f"{name}必须为非空字符串")
    if settings["asset_id"] is not None and not isinstance(settings["asset_id"], str):
        raise ValueError("asset_id必须为字符串或null")
    if settings["gpu"] != "cpu" and not settings["gpu"].isdigit():
        raise ValueError("gpu必须为单个物理GPU编号或cpu")
    if type(settings["port"]) is not int or not 1 <= settings["port"] <= 65535:
        raise ValueError("port必须是1~65535的整数")
    host = settings["host"]
    if any(char.isspace() for char in host) or "/" in host:
        raise ValueError(f"host={host!r}不是有效的监听地址；内网直连请填0.0.0.0，"
                         "不要填写ws://、端口、路径或空格；端口单独填port")
    # Resolve the binding address before loading/compiling the model. This does
    # not open a listening socket, and check-only still never loads the model.
    try:
        socket.getaddrinfo(host, settings["port"], type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    except socket.gaierror as exc:
        raise ValueError(f"无法解析监听host={host!r}、port={settings['port']}：{exc}；"
                         "内网直连可设置host为0.0.0.0，port单独填写8001") from exc
    if type(settings["num_steps"]) is not int or settings["num_steps"] <= 0:
        raise ValueError("num_steps必须是正整数")
    timeout = settings["inference_timeout_s"]
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("inference_timeout_s必须为正有限数")
    return args, settings


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        args, settings = settings_from_args(argv)
        logging.info("生效配置：config=%s，host=%r，port=%s，gpu=%s，checkpoint=%s",
                     args.config.expanduser().resolve(), settings["host"], settings["port"],
                     settings["gpu"], settings["checkpoint"])
        from .model import load_policy, select_checkpoint
        root = args.openpi_root.expanduser().resolve()
        if args.check_only:
            path = Path(settings["checkpoint"]).expanduser()
            path, asset = select_checkpoint(path if path.is_absolute() else root / path, settings["asset_id"])
            print(f"Checkpoint目录与归一化统计已找到：{path}\nasset_id={asset}；未加载模型，未启动服务。")
            return
        policy, metadata = load_policy(root, settings)
        metadata["default_prompt"] = settings["default_prompt"]
        server = PolicyServer(policy, metadata, inference_timeout_s=settings["inference_timeout_s"],
                              token=os.environ.get("RM65_INFER_TOKEN"))
        asyncio.run(server.run(settings["host"], settings["port"]))
    except KeyboardInterrupt:
        logging.info("推理服务已退出")
    except Exception as exc:
        logging.error("启动/运行失败：%s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
