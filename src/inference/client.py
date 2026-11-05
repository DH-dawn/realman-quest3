#!/usr/bin/env python3
"""Local-PC async policy client and a continuous NPZ communication example."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import math
import os
from pathlib import Path
import sys
import time

from .protocol import (ACTION_NAMES, ACTION_SEMANTICS, ACTION_TIMING, MAX_MESSAGE_BYTES,
                       PROTOCOL, dumps, encode_request, load_observation, loads, validate_actions)


class StaleObservationError(ValueError):
    """Input expired before socket.send; the existing connection is still usable."""


class PolicyClient:
    """One outstanding request; failures invalidate the connection, without automatic retry."""

    def __init__(self, uri="ws://127.0.0.1:8000", *, timeout_s=5., token=None):
        if not uri.startswith(("ws://", "wss://")):
            raise ValueError("uri需要ws://或wss://前缀")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s必须为正有限数")
        self.uri, self.timeout, self.token = uri, timeout_s, token
        self.socket = None
        self.metadata = None
        self.sequence = 0
        self.busy = False

    async def connect(self):
        from websockets.asyncio.client import connect

        if self.socket is not None:
            raise RuntimeError("客户端已连接，不重复连接")
        headers = {"Authorization": "Bearer " + self.token} if self.token else None
        self.socket = await connect(self.uri, additional_headers=headers, open_timeout=self.timeout,
            close_timeout=1., compression=None, max_size=MAX_MESSAGE_BYTES, max_queue=1,
            ping_interval=20, ping_timeout=20)
        try:
            hello = loads(await asyncio.wait_for(self.socket.recv(), self.timeout))
            if hello.get("type") != "hello" or hello.get("protocol") != PROTOCOL:
                raise RuntimeError(f"服务未就绪或协议不匹配：{hello}")
            horizon = hello.get("action_horizon")
            if type(horizon) is not int or not 1 <= horizon <= 1000 or not isinstance(hello.get("session_id"), str):
                raise ValueError("服务器握手缺少有效的动作长度或会话ID")
            if hello.get("state_dim") != 7 or hello.get("image_shape") != [224, 224, 3]:
                raise ValueError("服务器输入规格不匹配")
            if (hello.get("action_names") != ACTION_NAMES or hello.get("sample_rate_hz") != 10
                    or hello.get("action_semantics") != ACTION_SEMANTICS
                    or hello.get("action_timing") != ACTION_TIMING):
                raise ValueError("云端动作顺序、单位或时间定义不匹配；请同步更新云端tools/infer")
            self.metadata = hello
            self.sequence = 0
            return hello
        except BaseException:
            await self.close()
            raise

    async def close(self):
        socket, self.socket = self.socket, None
        self.metadata = None
        if socket is not None:
            await socket.close()

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def infer(self, observation, *, captured_at=None, max_observation_age_s=.1):
        """captured_at is PC monotonic time, never compared with the cloud's clock.

        Set captured_at for live observations. NPZ replay leaves it None and does
        not claim that the observation is fresh. Result age is returned for the
        future actuator controller to enforce its own execution deadline.
        """
        if self.socket is None or self.metadata is None:
            raise RuntimeError("客户端未连接；需要调用方显式建立新连接")
        if self.busy:
            raise RuntimeError("已有在途推理，不排队第二条请求")
        started = time.monotonic()
        if not math.isfinite(max_observation_age_s) or max_observation_age_s <= 0:
            raise ValueError("max_observation_age_s必须为正有限数")
        if captured_at is not None:
            if not math.isfinite(captured_at) or started < captured_at:
                raise ValueError("本地观测时间无效，本次不发送")
            if started - captured_at > max_observation_age_s:
                raise StaleObservationError("本地观测已过期，丢弃该帧并保留连接")
        self.busy = True
        self.sequence += 1
        request_id = self.sequence
        socket = self.socket
        metadata = self.metadata
        try:
            message = encode_request(observation, request_id)
            remaining = self.timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("准备请求时已超时")
            # 图片打包也计入本地观测年龄；不能用重新标记时间使旧观测变新。
            if captured_at is not None and time.monotonic() - captured_at > max_observation_age_s:
                raise StaleObservationError("请求编码后观测已过期，丢弃该帧并保留连接")

            async def exchange():
                await socket.send(message)
                return await socket.recv()

            response = loads(await asyncio.wait_for(exchange(), remaining))
            elapsed = time.monotonic() - started
            if elapsed > self.timeout:
                raise TimeoutError("推理结果到达时已超时，丢弃")
            if (response.get("protocol") != PROTOCOL or response.get("session_id") != metadata["session_id"]
                    or response.get("request_id") != request_id):
                raise RuntimeError("响应会话或request_id不匹配，丢弃结果")
            if response.get("type") == "error":
                raise RuntimeError(f"服务器拒绝请求：{response.get('code')}：{response.get('message')}")
            if response.get("type") != "result":
                raise RuntimeError("响应类型错误")
            if response.get("sample_rate_hz") != 10 or response.get("action_timing") != ACTION_TIMING:
                raise RuntimeError("响应动作时间定义不匹配，丢弃结果")
            response["actions"] = validate_actions(response.get("actions"), metadata["action_horizon"])
            response["client_timing"] = {"round_trip_ms": elapsed * 1000,
                "observation_age_ms": None if captured_at is None else (time.monotonic() - captured_at) * 1000}
            return response
        except StaleObservationError:
            # No bytes of this request have been sent; no late reply can exist.
            raise
        except asyncio.TimeoutError as exc:
            await self.close()
            raise TimeoutError(f"推理请求超过{self.timeout:g}秒，连接已关闭，迟到结果将丢弃") from exc
        except BaseException:
            # 已发出的推理不能撤回；关闭连接后不将迟到结果留给下一次调用。
            await self.close()
            raise
        finally:
            self.busy = False


async def run_example(args):
    output = args.output.expanduser().resolve() if args.output else Path("inference_outputs") / datetime.now().strftime("remote_%Y%m%d_%H%M%S_%f.jsonl")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        async with PolicyClient(args.uri, timeout_s=args.timeout, token=os.environ.get("RM65_INFER_TOKEN")) as client:
            print(f"已连接：{args.uri}\nCheckpoint：{client.metadata['checkpoint']}\n通信示例：读取NPZ，不采集实时相机，不控制机械臂。", flush=True)
            index = 0
            while args.count == 0 or index < args.count:
                observation = load_observation(args.input.expanduser())
                result = await client.infer(observation)
                result["actions"] = result["actions"].tolist()
                stream.write(dumps(result) + "\n")
                stream.flush()
                index += 1
                print(f"请求{result['request_id']}：{len(result['actions'])}×7，往返{result['client_timing']['round_trip_ms']:.1f}ms；首步={result['actions'][0]}", flush=True)
                if args.count == 0 or index < args.count:
                    await asyncio.sleep(args.interval)
    print(f"预测已保存：{output}", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uri", default="ws://127.0.0.1:8000")
    parser.add_argument("--input", type=Path, required=True, help="server_infer.py --prepare-only生成的observation.npz")
    parser.add_argument("--count", type=int, default=1, help="请求次数；0表示持续运行到Ctrl+C")
    parser.add_argument("--interval", type=float, default=.1, help="上次响应后等待秒数，不代表固定10Hz")
    parser.add_argument("--timeout", type=float, default=5., help="单次请求总超时秒数")
    parser.add_argument("--output", type=Path, help="新建JSONL文件，不覆盖已有文件")
    args = parser.parse_args(argv)
    if args.count < 0 or not math.isfinite(args.interval) or args.interval < 0:
        parser.error("count和interval必须非负且有限")
    try:
        asyncio.run(run_example(args))
    except KeyboardInterrupt:
        print("通信客户端已退出。")
    except Exception as exc:
        print(f"通信失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
