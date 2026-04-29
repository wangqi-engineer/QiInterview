"""火山引擎语音 WebSocket V3 协议帧封装。

参考：openspeech.bytedance.com /api/v3/tts/bidirection 与 /api/v3/sauc/bigmodel
帧结构：4 字节 header + 4 字节事件 ID + 4 字节 connect/session id 长度 + ID + 4 字节 payload 长度 + payload
本实现只用最常见的"完整客户端请求 + JSON payload"，足够覆盖业务需求。
"""
from __future__ import annotations

import json
import struct
import uuid
from enum import IntEnum
from typing import Any


# ===== 常量 =====
PROTOCOL_VERSION = 0b0001
HEADER_SIZE = 0b0001  # 4 字节
JSON_SERIALIZATION = 0b0001
NO_COMPRESSION = 0b0000

# Message types
FULL_CLIENT_REQUEST = 0b0001
AUDIO_ONLY_REQUEST = 0b0010
FULL_SERVER_RESPONSE = 0b1001
AUDIO_ONLY_RESPONSE = 0b1011
ERROR_RESPONSE = 0b1111

# Message type specific flags
NO_SEQUENCE = 0b0000
POS_SEQUENCE = 0b0001
NEG_SEQUENCE = 0b0010
NEG_WITH_SEQUENCE = 0b0011
WITH_EVENT = 0b0100


class TTSEvent(IntEnum):
    StartConnection = 1
    FinishConnection = 2
    ConnectionStarted = 50
    ConnectionFailed = 51
    ConnectionFinished = 52

    StartSession = 100
    FinishSession = 102
    SessionStarted = 150
    SessionFinished = 152
    SessionFailed = 153

    TaskRequest = 200

    TTSSentenceStart = 350
    TTSSentenceEnd = 351
    TTSResponse = 352  # 音频数据
    TTSEnded = 359


class ASREvent(IntEnum):
    StartConnection = 1
    FinishConnection = 2
    ConnectionStarted = 50
    ConnectionFailed = 51

    StartSession = 100
    FinishSession = 102
    SessionStarted = 150
    SessionFinished = 152
    SessionFailed = 153

    TaskRequest = 200
    TaskResponse = 451


def _build_header(
    message_type: int,
    flags: int,
    serialization: int = JSON_SERIALIZATION,
    compression: int = NO_COMPRESSION,
) -> bytes:
    return bytes(
        [
            (PROTOCOL_VERSION << 4) | HEADER_SIZE,
            (message_type << 4) | flags,
            (serialization << 4) | compression,
            0,
        ]
    )


def build_event_payload(
    event: int,
    session_id: str,
    payload: bytes,
    *,
    message_type: int = FULL_CLIENT_REQUEST,
    include_session_id: bool = True,
) -> bytes:
    """构造一个完整的协议帧（带 event id）。"""
    flags = WITH_EVENT
    header = _build_header(message_type, flags)
    out = bytearray()
    out += header
    out += struct.pack(">I", event)
    if include_session_id:
        sid_bytes = session_id.encode("utf-8")
        out += struct.pack(">I", len(sid_bytes))
        out += sid_bytes
    out += struct.pack(">I", len(payload))
    out += payload
    return bytes(out)


def build_audio_only_payload(
    event: int, session_id: str, audio: bytes
) -> bytes:
    """音频数据帧。"""
    flags = WITH_EVENT
    header = _build_header(AUDIO_ONLY_REQUEST, flags, serialization=0, compression=NO_COMPRESSION)
    out = bytearray()
    out += header
    out += struct.pack(">I", event)
    sid_bytes = session_id.encode("utf-8")
    out += struct.pack(">I", len(sid_bytes))
    out += sid_bytes
    out += struct.pack(">I", len(audio))
    out += audio
    return bytes(out)


def parse_response(data: bytes) -> dict[str, Any]:
    """解析服务端返回帧。
    返回：{message_type, event, session_id, payload(bytes), payload_json(可选)}
    """
    if len(data) < 4:
        return {"error": "frame too short"}
    proto_ver_size = data[0]
    msg_type_flags = data[1]
    serialization_compression = data[2]
    # data[3] reserved

    message_type = (msg_type_flags >> 4) & 0x0F
    flags = msg_type_flags & 0x0F
    serialization = (serialization_compression >> 4) & 0x0F

    pos = 4
    event = 0
    if flags & WITH_EVENT:
        if len(data) < pos + 4:
            return {"error": "no event id"}
        (event,) = struct.unpack(">I", data[pos : pos + 4])
        pos += 4

    session_id = ""
    if message_type in (FULL_SERVER_RESPONSE, AUDIO_ONLY_RESPONSE, FULL_CLIENT_REQUEST, AUDIO_ONLY_REQUEST):
        # 跟事件 id 后通常有 session_id
        if event >= 50 and len(data) >= pos + 4:
            (sid_len,) = struct.unpack(">I", data[pos : pos + 4])
            pos += 4
            session_id = data[pos : pos + sid_len].decode("utf-8", errors="replace")
            pos += sid_len

    payload = b""
    if message_type == ERROR_RESPONSE:
        # error: 4 字节错误码 + 4 字节 msg 长度 + msg
        if len(data) >= pos + 8:
            (_, msg_len) = struct.unpack(">II", data[pos : pos + 8])
            pos += 8
            payload = data[pos : pos + msg_len]
    else:
        if len(data) >= pos + 4:
            (payload_len,) = struct.unpack(">I", data[pos : pos + 4])
            pos += 4
            payload = data[pos : pos + payload_len]

    out: dict[str, Any] = {
        "message_type": message_type,
        "event": event,
        "session_id": session_id,
        "payload": payload,
    }
    if serialization == JSON_SERIALIZATION and payload:
        try:
            out["payload_json"] = json.loads(payload.decode("utf-8"))
        except Exception:
            pass
    return out


def new_session_id() -> str:
    return uuid.uuid4().hex


def new_connect_id() -> str:
    return uuid.uuid4().hex


# 高层封装：构造常用帧
TTS_NAMESPACE = "BidirectionalTTS"


def start_connection_frame() -> bytes:
    return build_event_payload(
        TTSEvent.StartConnection.value, "", b"{}", include_session_id=False
    )


def finish_connection_frame() -> bytes:
    return build_event_payload(
        TTSEvent.FinishConnection.value, "", b"{}", include_session_id=False
    )


def start_tts_session_frame(session_id: str, req_params: dict) -> bytes:
    payload = json.dumps(
        {
            "event": int(TTSEvent.StartSession),
            "namespace": TTS_NAMESPACE,
            "req_params": req_params,
        }
    ).encode("utf-8")
    return build_event_payload(TTSEvent.StartSession.value, session_id, payload)


def tts_task_request_frame(
    session_id: str, text: str, req_params_overrides: dict | None = None
) -> bytes:
    payload_dict: dict[str, Any] = {
        "event": int(TTSEvent.TaskRequest),
        "namespace": TTS_NAMESPACE,
        "req_params": {"text": text},
    }
    if req_params_overrides:
        payload_dict["req_params"].update(req_params_overrides)
    return build_event_payload(
        TTSEvent.TaskRequest.value, session_id, json.dumps(payload_dict).encode("utf-8")
    )


def finish_tts_session_frame(session_id: str) -> bytes:
    payload = json.dumps(
        {"event": int(TTSEvent.FinishSession), "namespace": TTS_NAMESPACE}
    ).encode("utf-8")
    return build_event_payload(TTSEvent.FinishSession.value, session_id, payload)


# ===== STT 高层封装 =====
ASR_NAMESPACE = "ASR"


def start_asr_session_frame(session_id: str, req_params: dict) -> bytes:
    """流式 ASR 的 StartSession 帧。``req_params`` 一般包含 model_name /
    audio (format/rate/bits/channel/codec) / request 等字段；body 与火山
    /api/v3/sauc/bigmodel 文档一致。"""
    payload = json.dumps(
        {
            "event": int(ASREvent.StartSession),
            "namespace": ASR_NAMESPACE,
            "req_params": req_params,
        }
    ).encode("utf-8")
    return build_event_payload(ASREvent.StartSession.value, session_id, payload)


def asr_task_request_audio_frame(session_id: str, audio: bytes) -> bytes:
    """ASR 音频上行帧。事件 id = TaskRequest，message_type = AUDIO_ONLY_REQUEST。"""
    return build_audio_only_payload(ASREvent.TaskRequest.value, session_id, audio)


def finish_asr_session_frame(session_id: str) -> bytes:
    payload = json.dumps(
        {"event": int(ASREvent.FinishSession), "namespace": ASR_NAMESPACE}
    ).encode("utf-8")
    return build_event_payload(ASREvent.FinishSession.value, session_id, payload)
