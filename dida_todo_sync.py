#!/usr/bin/env python3
"""
定时同步滴答(通过 webcal/ics)到墨水屏待办接口。

功能：
1. 定时拉取滴答 ICS（VTODO）。
2. 按 api.json 的待办接口进行创建/更新/完成/删除同步。
3. 将 UID 与云端 todoId 的映射保存到本地，避免重复创建。

运行前可通过环境变量配置（也可直接改默认值）：
- CLOUD_BASE_URL
- CLOUD_API_KEY
- CLOUD_API_KEY_HEADER（默认：X-API-Key）
- CLOUD_AUTH_SCHEME（可选：Bearer）
- DEVICE_ID
- DIDA_ICS_URL
- SYNC_STATE_FILE（默认：sync_state.json）
- SYNC_INTERVAL_SECONDS（默认：300）
- REQUEST_TIMEOUT_SECONDS（默认：15）
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_BASE_URL = "https://cloud.zectrix.com"
DEFAULT_DEVICE_ID = ""
DEFAULT_API_KEY = ""
DEFAULT_DIDA_ICS_URL = ""
SYNC_MARKER_PREFIX = "[SYNC_UID:"
SYNC_MARKER_SUFFIX = "]"


def now_str() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"[{now_str()}] {msg}", flush=True)


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


@dataclass
class Config:
    base_url: str
    api_key: str
    api_key_header: str
    auth_scheme: Optional[str]
    device_id: str
    dida_ics_url: str
    state_file: str
    sync_interval_seconds: int
    timeout_seconds: int

    @classmethod
    def from_sources(cls, args: argparse.Namespace) -> "Config":
        def pick(cli_value: Optional[str], env_name: str, default: str = "") -> str:
            if cli_value is not None and cli_value != "":
                return cli_value
            return os.getenv(env_name, default)

        config = cls(
            base_url=pick(args.base_url, "CLOUD_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            api_key=pick(args.api_key, "CLOUD_API_KEY", DEFAULT_API_KEY),
            api_key_header=pick(args.api_key_header, "CLOUD_API_KEY_HEADER", "X-API-Key"),
            auth_scheme=pick(args.auth_scheme, "CLOUD_AUTH_SCHEME", "") or None,
            device_id=pick(args.device_id, "DEVICE_ID", DEFAULT_DEVICE_ID),
            dida_ics_url=pick(args.dida_ics_url, "DIDA_ICS_URL", DEFAULT_DIDA_ICS_URL),
            state_file=pick(args.state_file, "SYNC_STATE_FILE", "sync_state.json"),
            sync_interval_seconds=args.sync_interval_seconds
            if args.sync_interval_seconds is not None
            else env_int("SYNC_INTERVAL_SECONDS", 300),
            timeout_seconds=args.timeout_seconds
            if args.timeout_seconds is not None
            else env_int("REQUEST_TIMEOUT_SECONDS", 15),
        )
        config.validate()
        return config

    def validate(self) -> None:
        missing: List[str] = []
        if not self.api_key.strip():
            missing.append("api_key/CLOUD_API_KEY")
        if not self.device_id.strip():
            missing.append("device_id/DEVICE_ID")
        if not self.dida_ics_url.strip():
            missing.append("dida_ics_url/DIDA_ICS_URL")
        if missing:
            raise ValueError(f"缺少必填配置: {', '.join(missing)}")


class CloudTodoClient:
    def __init__(self, config: Config):
        self.config = config

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.auth_scheme:
            headers["Authorization"] = f"{self.config.auth_scheme} {self.config.api_key}"
        else:
            headers[self.config.api_key_header] = self.config.api_key
        return headers

    def _request(
        self,
        method: str,
        path: str,
        query: Optional[Dict[str, Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.config.base_url}{path}"
        if query:
            query = {k: v for k, v in query.items() if v is not None}
            if query:
                url = f"{url}?{urllib.parse.urlencode(query)}"

        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url=url, data=data, headers=self._headers(), method=method)

        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                text = resp.read().decode("utf-8", errors="replace")
                if not text.strip():
                    return {"code": 0, "data": None}
                return json.loads(text)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code} {method} {path} -> {body}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"网络请求失败 {method} {path}: {e}") from e

    def _expect_ok(self, response: Dict[str, Any], action: str) -> Any:
        code = response.get("code")
        if code != 0:
            raise RuntimeError(f"{action} 失败: code={code}, resp={response}")
        return response.get("data")

    def list_todos(self, device_id: Optional[str] = None, status: Optional[int] = None) -> List[Dict[str, Any]]:
        data = self._expect_ok(
            self._request("GET", "/open/v1/todos", query={"deviceId": device_id, "status": status}),
            "获取待办列表",
        )
        return data or []

    def create_todo(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = self._expect_ok(self._request("POST", "/open/v1/todos", payload=payload), "创建待办")
        return data or {}

    def update_todo(self, todo_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = self._expect_ok(
            self._request("PUT", f"/open/v1/todos/{todo_id}", payload=payload),
            f"更新待办#{todo_id}",
        )
        return data or {}

    def toggle_complete(self, todo_id: int) -> None:
        self._expect_ok(
            self._request("PUT", f"/open/v1/todos/{todo_id}/complete"),
            f"切换完成状态#{todo_id}",
        )

    def delete_todo(self, todo_id: int) -> None:
        self._expect_ok(self._request("DELETE", f"/open/v1/todos/{todo_id}"), f"删除待办#{todo_id}")


def normalize_webcal_url(url: str) -> str:
    if url.startswith("webcal://"):
        return "https://" + url[len("webcal://") :]
    return url


def fetch_ics_text(url: str, timeout_seconds: int) -> str:
    request = urllib.request.Request(normalize_webcal_url(url), method="GET")
    with urllib.request.urlopen(request, timeout=timeout_seconds) as resp:
        return resp.read().decode("utf-8", errors="replace")


def unfold_ics_lines(ics_text: str) -> List[str]:
    raw_lines = ics_text.splitlines()
    if not raw_lines:
        return []
    lines: List[str] = []
    for line in raw_lines:
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def unescape_ics_text(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .strip()
    )


def parse_ics_datetime(value: str) -> Tuple[Optional[str], Optional[str]]:
    value = value.strip()
    if not value:
        return None, None

    if "T" in value:
        core = value.rstrip("Z")
        try:
            dt_obj = dt.datetime.strptime(core[:15], "%Y%m%dT%H%M%S")
            return dt_obj.strftime("%Y-%m-%d"), dt_obj.strftime("%H:%M")
        except ValueError:
            pass
        return None, None

    try:
        date_obj = dt.datetime.strptime(value[:8], "%Y%m%d").date()
        return date_obj.strftime("%Y-%m-%d"), None
    except ValueError:
        return None, None


def parse_priority(ics_priority: Optional[str]) -> int:
    if not ics_priority:
        return 0
    try:
        p = int(ics_priority.strip())
    except ValueError:
        return 0
    if p <= 3:
        return 2
    if p <= 6:
        return 1
    return 0


def extract_uid_from_description(description: str) -> Optional[str]:
    if not description:
        return None
    marker_start = description.find(SYNC_MARKER_PREFIX)
    if marker_start < 0:
        return None
    marker_end = description.find(SYNC_MARKER_SUFFIX, marker_start)
    if marker_end < 0:
        return None
    uid = description[marker_start + len(SYNC_MARKER_PREFIX) : marker_end].strip()
    return uid or None


def merge_description_with_uid(description: str, uid: str) -> str:
    cleaned = description.strip()
    marker = f"{SYNC_MARKER_PREFIX}{uid}{SYNC_MARKER_SUFFIX}"
    if marker in cleaned:
        return cleaned
    if cleaned:
        return f"{cleaned}\n\n{marker}"
    return marker


def build_hash(item: Dict[str, Any]) -> str:
    payload = {
        "title": item.get("title"),
        "description": item.get("description"),
        "dueDate": item.get("dueDate"),
        "dueTime": item.get("dueTime"),
        "priority": item.get("priority"),
        "completed": bool(item.get("completed")),
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_todos_from_ics(ics_text: str) -> Dict[str, Dict[str, Any]]:
    lines = unfold_ics_lines(ics_text)
    todos: Dict[str, Dict[str, Any]] = {}
    current_component: Optional[str] = None
    current: Dict[str, str] = {}

    def flush_current() -> None:
        uid = current.get("UID", "").strip()
        title = unescape_ics_text(current.get("SUMMARY", ""))
        if not uid or not title:
            return
        description = unescape_ics_text(current.get("DESCRIPTION", ""))
        # TickTick 的 webcal 常见为 VEVENT（DTSTART），非 VTODO（DUE）。
        due_raw = current.get("DUE", "") or current.get("DTSTART", "")
        due_date, due_time = parse_ics_datetime(due_raw)
        completed = current.get("STATUS", "").upper() == "COMPLETED"
        priority = parse_priority(current.get("PRIORITY"))

        item = {
            "uid": uid,
            "title": title[:200],
            "description": description,
            "dueDate": due_date,
            "dueTime": due_time,
            "repeatType": "none",
            "priority": priority,
            "completed": completed,
        }
        item["hash"] = build_hash(item)
        todos[uid] = item

    for line in lines:
        line = line.rstrip("\r")
        if line in ("BEGIN:VTODO", "BEGIN:VEVENT"):
            current_component = line.split(":", 1)[1]
            current = {}
            continue
        if line in ("END:VTODO", "END:VEVENT"):
            ending_component = line.split(":", 1)[1]
            if current_component and current_component == ending_component:
                flush_current()
            current_component = None
            current = {}
            continue
        if not current_component or ":" not in line:
            continue

        key_and_params, value = line.split(":", 1)
        key = key_and_params.split(";", 1)[0].upper()
        current[key] = value

    return todos


def load_state(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"uid_to_todo_id": {}, "last_hash": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return {"uid_to_todo_id": {}, "last_hash": {}}
            data.setdefault("uid_to_todo_id", {})
            data.setdefault("last_hash", {})
            return data
    except (OSError, json.JSONDecodeError):
        return {"uid_to_todo_id": {}, "last_hash": {}}


def save_state(path: str, state: Dict[str, Any]) -> None:
    state["last_sync_at"] = now_str()
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def normalize_cloud_todo(todo: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": int(todo.get("id")),
        "title": (todo.get("title") or "").strip(),
        "description": todo.get("description") or "",
        "dueDate": todo.get("dueDate"),
        "dueTime": todo.get("dueTime"),
        "priority": int(todo.get("priority") or 0),
        "completed": bool(todo.get("completed")),
    }


def is_same_payload(dida_item: Dict[str, Any], cloud_item: Dict[str, Any]) -> bool:
    cloud_description = cloud_item.get("description", "")
    uid = dida_item["uid"]
    cloud_uid = extract_uid_from_description(cloud_description)
    if cloud_uid and cloud_uid != uid:
        return False

    expected_description = merge_description_with_uid(dida_item.get("description", ""), uid)
    return (
        dida_item.get("title") == cloud_item.get("title")
        and expected_description == cloud_description.strip()
        and dida_item.get("dueDate") == cloud_item.get("dueDate")
        and dida_item.get("dueTime") == cloud_item.get("dueTime")
        and int(dida_item.get("priority", 0)) == int(cloud_item.get("priority", 0))
    )


def sync_once(config: Config, client: CloudTodoClient) -> None:
    log("开始同步...")
    ics_text = fetch_ics_text(config.dida_ics_url, config.timeout_seconds)
    dida_todos = parse_todos_from_ics(ics_text)
    log(f"滴答待办数量(VTODO/VEVENT): {len(dida_todos)}")

    cloud_raw = client.list_todos(device_id=config.device_id)
    cloud_todos = [normalize_cloud_todo(t) for t in cloud_raw]
    cloud_by_id = {t["id"]: t for t in cloud_todos}
    cloud_id_by_uid: Dict[str, int] = {}
    for todo in cloud_todos:
        uid = extract_uid_from_description(todo.get("description", ""))
        if uid:
            cloud_id_by_uid[uid] = todo["id"]

    state = load_state(config.state_file)
    uid_to_todo_id: Dict[str, int] = {
        k: int(v) for k, v in state.get("uid_to_todo_id", {}).items() if str(v).isdigit()
    }
    last_hash: Dict[str, str] = dict(state.get("last_hash", {}))

    created = 0
    updated = 0
    completed_switch = 0
    deleted = 0

    seen_uids = set()

    for uid, item in dida_todos.items():
        seen_uids.add(uid)
        todo_id = uid_to_todo_id.get(uid) or cloud_id_by_uid.get(uid)
        cloud_item = cloud_by_id.get(todo_id) if todo_id else None

        payload = {
            "title": item["title"],
            "description": merge_description_with_uid(item.get("description", ""), uid),
            "dueDate": item.get("dueDate"),
            "dueTime": item.get("dueTime"),
            "repeatType": "none",
            "priority": item.get("priority", 0),
            "deviceId": config.device_id,
        }
        payload = {k: v for k, v in payload.items() if v is not None}

        if cloud_item is None:
            created_item = client.create_todo(payload)
            new_id = int(created_item["id"])
            uid_to_todo_id[uid] = new_id
            cloud_by_id[new_id] = normalize_cloud_todo(
                {
                    "id": new_id,
                    "title": payload.get("title"),
                    "description": payload.get("description"),
                    "dueDate": payload.get("dueDate"),
                    "dueTime": payload.get("dueTime"),
                    "priority": payload.get("priority"),
                    "completed": False,
                }
            )
            created += 1
            log(f"创建待办: {item['title']} (id={new_id})")
            cloud_item = cloud_by_id[new_id]
        else:
            uid_to_todo_id[uid] = int(cloud_item["id"])
            need_update = (last_hash.get(uid) != item["hash"]) or (not is_same_payload(item, cloud_item))
            if need_update:
                update_payload = {
                    "title": payload.get("title"),
                    "description": payload.get("description"),
                    "dueDate": payload.get("dueDate"),
                    "dueTime": payload.get("dueTime"),
                    "priority": payload.get("priority"),
                }
                client.update_todo(cloud_item["id"], update_payload)
                updated += 1
                log(f"更新待办: {item['title']} (id={cloud_item['id']})")
                cloud_item = {
                    **cloud_item,
                    "title": update_payload.get("title"),
                    "description": update_payload.get("description"),
                    "dueDate": update_payload.get("dueDate"),
                    "dueTime": update_payload.get("dueTime"),
                    "priority": update_payload.get("priority"),
                }
                cloud_by_id[cloud_item["id"]] = cloud_item

        want_completed = bool(item.get("completed"))
        have_completed = bool(cloud_item.get("completed"))
        if want_completed != have_completed:
            client.toggle_complete(int(cloud_item["id"]))
            completed_switch += 1
            cloud_item["completed"] = want_completed
            cloud_by_id[cloud_item["id"]] = cloud_item
            log(
                f"切换完成状态: {item['title']} (id={cloud_item['id']}, completed={want_completed})"
            )

        last_hash[uid] = item["hash"]

    for uid, todo_id in list(uid_to_todo_id.items()):
        if uid in seen_uids:
            continue
        if todo_id in cloud_by_id:
            client.delete_todo(todo_id)
            deleted += 1
            log(f"删除已移除待办: uid={uid}, id={todo_id}")
        uid_to_todo_id.pop(uid, None)
        last_hash.pop(uid, None)

    new_state = {
        "uid_to_todo_id": uid_to_todo_id,
        "last_hash": last_hash,
    }
    save_state(config.state_file, new_state)
    log(
        "同步完成: "
        f"created={created}, updated={updated}, toggled={completed_switch}, deleted={deleted}"
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="定时同步滴答待办到墨水屏云端待办接口"
    )
    parser.add_argument("--base-url", help="云端 API 地址，如 https://cloud.zectrix.com")
    parser.add_argument("--api-key", help="云端 API Key")
    parser.add_argument("--api-key-header", help="API Key 请求头名称，默认 X-API-Key")
    parser.add_argument("--auth-scheme", help="可选鉴权方案，如 Bearer（走 Authorization）")
    parser.add_argument("--device-id", help="目标设备 ID(MAC)")
    parser.add_argument("--dida-ics-url", help="滴答 webcal/ics 地址")
    parser.add_argument("--state-file", help="状态文件路径，默认 sync_state.json")
    parser.add_argument("--sync-interval-seconds", type=int, help="同步间隔秒数，默认 300")
    parser.add_argument("--timeout-seconds", type=int, help="请求超时秒数，默认 15")
    parser.add_argument(
        "--once",
        action="store_true",
        help="只同步一次后退出（默认会循环定时同步）",
    )
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    config = Config.from_sources(args)
    client = CloudTodoClient(config)

    log(
        f"启动同步器: base={config.base_url}, device={config.device_id}, "
        f"interval={config.sync_interval_seconds}s, state={config.state_file}"
    )

    if args.once:
        sync_once(config, client)
        return 0

    while True:
        try:
            sync_once(config, client)
        except Exception as exc:
            log(f"同步失败: {exc}")
        time.sleep(max(5, config.sync_interval_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
