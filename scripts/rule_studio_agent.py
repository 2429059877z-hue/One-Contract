#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 AI Agent 用的 Rule Studio 快捷入口。

**为什么单独有这个脚本**：`open_rule_studio.py` 是给人用的，会打开浏览器；
Agent 通常需要后台启动、等待就绪并以 API 方式调用。本脚本封装这些操作，
会话凭据由同源 bootstrap 临时取得，不进入 URL、命令行或状态文件。

它复用 `open_rule_studio.py` 的本地服务，不引入第二套规则解析逻辑。

用法
----
    python scripts/rule_studio_agent.py start [--idle-seconds 1800]
        启动服务（不打开浏览器），等就绪后打印 URL 与 PID。
        **token 不打印、不落盘**——需要时由 `api` 子命令在内存中换取。

    python scripts/rule_studio_agent.py api <PATH> [--method GET] [--data-file request.json]
        对运行中的实例调用 API。脚本内部完成 bootstrap，自动携带 token、
        CSRF 与同源信息，**会话凭据不经过调用方**。
        --data-file - 从标准输入读取 UTF-8 JSON；与 --data 互斥。

    python scripts/rule_studio_agent.py status
    python scripts/rule_studio_agent.py stop

    python scripts/rule_studio_agent.py request get <request_id> [--client-profile-id <id>]
    python scripts/rule_studio_agent.py schema <kind>
    python scripts/rule_studio_agent.py request submit <request_id> --data-file candidate.json
        读取要求与反馈，提交外部 AI 已完成的候选。不会自动生成、采用或启用规则。

合规
----
- 运行状态以 0600 权限写在系统临时目录（仅 url、pid 与日志路径），**不含会话凭据**——
  遵循 architecture-contract §13「token 不放入持久配置或普通日志」；
- 停止服务走本地认证 API；状态过期时不按旧 PID 发信号，避免 PID 复用误伤；
- 仅通过本机 API 执行请求，不直接改写规则资产；不连接外部服务。

退出码：0 成功；1 失败；2 用法错误。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

_HERE = pathlib.Path(__file__).resolve().parent
_LAUNCHER = _HERE / "open_rule_studio.py"
_INSTALL_ID = hashlib.sha256(str(_HERE).encode("utf-8")).hexdigest()[:16]
_STATE = pathlib.Path(tempfile.gettempdir()) / ("one-contract-studio-" + _INSTALL_ID) / "agent.json"
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def _load_state():
    try:
        return json.loads(_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_state(d):
    from one_contract_rules import private_fs
    private_fs.atomic_write_json(_STATE, d)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "本机服务不允许重定向。", headers, fp)


def _http(url, *, method="GET", token=None, csrf=None, origin=None,
          data=None, timeout=10):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "::1")
            or parsed.username or parsed.password or not parsed.port):
        raise ValueError("Rule Studio Agent 只允许访问本机回环 HTTP 地址。")
    req = urllib.request.Request(url, method=method)
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if csrf:
        req.add_header("X-CSRF-Token", csrf)
    if origin:
        req.add_header("Origin", origin)
    body = None
    if data is not None:
        body = data.encode("utf-8")
        req.add_header("Content-Type", "application/json")
    # 本地 API 不应经过系统 HTTP(S) 代理。某些企业环境没有把
    # 127.0.0.1 写入 NO_PROXY，urllib 会将请求错送到代理并超时。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(req, data=body, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def _baseline(url, timeout=1.5):
    """就绪探针——判断服务是否已可用。

    **不能用 `/api/v1/health`**：它需要会话凭据，无 token 时返回 401，
    拿它做探针会永远判定"未就绪"。会话 bootstrap 端点才是无凭据可用的，
    且"能 bootstrap"本身就等价于服务就绪。
    """
    try:
        _, d = _http(f"{url}api/v1/session/bootstrap", timeout=timeout)
        return bool(d.get("success"))
    except Exception:
        return False


def cmd_start(args) -> int:
    st = _load_state()
    if st and _baseline(st.get("url", "")):
        requested_root = str(pathlib.Path(args.data_dir).expanduser().resolve()) if getattr(args, "data_dir", None) else None
        if (st.get("data_dir") != requested_root
                or bool(st.get("read_only")) != bool(getattr(args, "read_only", False))):
            print("已有实例的数据目录或只读模式与本次不同，请先 stop 或使用 restart。", file=sys.stderr)
            return 1
        print(f"已在运行：{st['url']}  (pid {st.get('pid')})")
        print(f"如需重启，先执行：{sys.argv[0]} stop")
        return 0

    from one_contract_rules import private_fs
    private_fs.secure_dir(_STATE.parent)
    log = _STATE.parent / "agent.log"
    private_fs.atomic_write_text(log, "")
    command = [sys.executable, "-u", str(_LAUNCHER), "--no-browser",
               "--idle-timeout-seconds", str(args.idle_seconds)]
    if getattr(args, "data_dir", None):
        command.extend(["--data-dir", args.data_dir])
    if getattr(args, "read_only", False):
        command.append("--read-only")
    spawn_options = _background_options()
    with open(log, "wb") as fh:
        proc = subprocess.Popen(
            command,
            stdout=fh, stderr=subprocess.STDOUT,
            cwd=str(_HERE.parent),
            env=dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8"),
            **spawn_options,
        )

    url = None
    metadata = {}
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            print(f"启动失败，退出码 {proc.returncode}。日志：{log}", file=sys.stderr)
            try:
                print(log.read_text(encoding="utf-8")[-800:], file=sys.stderr)
            except OSError:
                pass
            return 1
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("{") and '"url"' in line:
                try:
                    metadata = json.loads(line)
                    url = metadata["url"]
                except ValueError:
                    pass
        if url and _baseline(url):
            break
        time.sleep(0.4)

    if not url:
        _stop_spawned(proc)
        print(f"超时未就绪。日志：{log}", file=sys.stderr)
        return 1
    if not _baseline(url, timeout=3):
        _stop_spawned(proc)
        print(f"服务已启动但健康检查未通过：{url}", file=sys.stderr)
        return 1

    try:
        _save_state({"url": url, "pid": proc.pid, "log": str(log),
                     "session_id": metadata.get("session_id"),
                     "data_dir": str(pathlib.Path(args.data_dir).expanduser().resolve()) if getattr(args, "data_dir", None) else None,
                     "read_only": bool(getattr(args, "read_only", False))})
    except OSError:
        _stop_spawned(proc)
        print("无法保存本次运行状态，已停止刚启动的实例。", file=sys.stderr)
        return 1
    if getattr(args, "open_browser", False):
        import webbrowser
        try:
            webbrowser.open(url)
        except Exception:
            pass
    print(f"已启动：{url}")
    print(f"  PID      : {proc.pid}")
    print(f"  日志     : {log}")
    print(f"  空闲自动退出: {args.idle_seconds} 秒")
    print()
    print("调 API（token 由脚本内部换取，不出现在命令行与输出里）：")
    print(f"  python {sys.argv[0]} api /api/v1/rules/tree")
    print()
    print("浏览器访问：直接把上面的 URL 贴进去即可——页面会自行完成会话初始化。")
    return 0


def _background_options(platform_name=None):
    if (platform_name or os.name) == "nt":
        return {"creationflags": 0x00000008 | 0x00000200}  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    return {"start_new_session": True}


def _stop_spawned(proc):
    """Only stop a process we just created, never a persisted PID."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _session_matches(state, credentials):
    return not state.get("session_id") or state["session_id"] == credentials.get("session_id")


def _request_data(args):
    """Read file/stdin as UTF-8 before contacting the service; never log its body."""
    filename = getattr(args, "data_file", None)
    if filename is None:
        return getattr(args, "data", None)
    if getattr(args, "data", None) is not None:
        raise ValueError("--data 与 --data-file 不能同时使用。")
    try:
        if filename == "-":
            stream = getattr(sys.stdin, "buffer", sys.stdin)
            content = stream.read()
            text = content.decode("utf-8-sig") if isinstance(content, bytes) else content.lstrip("\ufeff")
        else:
            text = pathlib.Path(filename).read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("请求文件或标准输入必须使用 UTF-8 编码。") from exc
    except OSError as exc:
        raise ValueError("无法读取请求文件或标准输入。") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"请求内容不是有效 JSON（第 {exc.lineno} 行，第 {exc.colno} 列）。") from exc
    return json.dumps(payload, ensure_ascii=False)


def _http_error_body(exc):
    text = exc.read().decode("utf-8", "replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def _explicit_url(value):
    """Accept only a plain, explicit loopback origin supplied by Studio/user."""
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
        valid = (isinstance(value, str) and not any(ord(c) <= 32 for c in value)
                 and parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "::1")
                 and port and parsed.username is None and parsed.password is None
                 and parsed.path in ("", "/") and "?" not in value and "#" not in value)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError("--url 仅接受带端口的本机回环 HTTP 地址，不得含账号、路径、查询参数或片段。")
    host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
    return f"http://{host}:{port}"


def cmd_api(args) -> int:
    try:
        data = _request_data(args)
        explicit = _explicit_url(args.url) if getattr(args, "url", None) is not None else None
    except ValueError as exc:
        print(f"请求未发送：{exc}", file=sys.stderr)
        return 2
    st = {"url": explicit} if explicit else _load_state()
    if not st:
        print("没有记录中的实例。工作台已打开时用 --url 指定其本机地址；需要新实例时再执行 start。", file=sys.stderr)
        return 1
    url = st["url"].rstrip("/")

    # bootstrap：无凭据端点换会话凭据（契约的设计路径）。token 只存活在本进程内。
    try:
        _, b = _http(f"{url}/api/v1/session/bootstrap")
    except Exception as exc:
        print(f"会话初始化失败：{exc}。服务可能已退出，试 status 或重新 start。", file=sys.stderr)
        return 1
    token = (b.get("result") or {}).get("token")
    csrf = (b.get("result") or {}).get("csrf_token")
    origin = (b.get("result") or {}).get("origin")
    if not token or not csrf or not origin:
        print("会话初始化未返回完整凭据；请重新启动工作台。", file=sys.stderr)
        return 1
    if not _session_matches(st, b.get("result") or {}):
        print("原工作台会话已失效；当前端口属于另一实例，请重新启动。", file=sys.stderr)
        return 1

    path = args.path if args.path.startswith("/") else "/" + args.path
    try:
        status, payload = _http(
            f"{url}{path}", method=args.method, token=token,
            csrf=csrf, origin=origin,
            data=data, timeout=args.timeout,
        )
    except urllib.error.HTTPError as exc:
        print(json.dumps({"http_status": exc.code,
                          "body": _http_error_body(exc)},
                         ensure_ascii=False, indent=2))
        return 1
    except Exception as exc:
        print(f"请求失败：{exc}", file=sys.stderr)
        return 1

    envelope = {"http_status": status, "body": payload}
    result = payload.get("result") if isinstance(payload, dict) and payload.get("success") else None
    if getattr(args, "request_link", False) and isinstance(result, dict):
        request_id = result.get("request_id")
        owner = result.get("client_profile_id")
        if isinstance(request_id, str) and request_id and (owner is None or isinstance(owner, str)):
            fragment = {"ai-request": request_id}
            if owner:
                fragment["client"] = owner
            envelope["studio_url"] = url + "/#" + urllib.parse.urlencode(fragment)
    print(json.dumps(envelope, ensure_ascii=False, indent=2))
    return 0


def cmd_status(_args) -> int:
    st = _load_state()
    if not st:
        print("未记录运行中的实例。")
        return 0
    alive = _baseline(st.get("url", ""), timeout=3)
    print(json.dumps({"url": st.get("url"), "pid": st.get("pid"),
                      "healthy": alive, "log": st.get("log")},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_stop(_args) -> int:
    st = _load_state()
    if not st:
        print("未记录运行中的实例。")
        return 0
    url = str(st.get("url") or "").rstrip("/")
    stopped = False
    if url and _baseline(url + "/", timeout=3):
        try:
            _, bootstrap = _http(f"{url}/api/v1/session/bootstrap")
            credentials = bootstrap.get("result") or {}
            if not _session_matches(st, credentials):
                print("端口已由另一工作台会话使用，未发送停止请求；已清理过期状态。")
                _STATE.unlink(missing_ok=True)
                return 0
            _http(
                f"{url}/api/v1/service/stop",
                method="POST",
                token=credentials.get("token"),
                csrf=credentials.get("csrf_token"),
                origin=credentials.get("origin"),
                data="{}",
                timeout=5,
            )
            stopped = True
        except Exception:
            # 服务可能在返回响应前完成关闭。只要端口已经不可达，就视为成功。
            stopped = not _baseline(url + "/", timeout=1)
        if stopped:
            print(f"已通过本地服务安全停止：{st.get('url')}")
        else:
            print("停止请求未生效；未向记录的 PID 发送信号，以免误伤其他进程。", file=sys.stderr)
            return 1
    else:
        print("服务已不可达；仅清理过期状态，不向记录的 PID 发送信号。")
    try:
        _STATE.unlink()
    except OSError:
        pass
    return 0


def cmd_restart(args):
    code = cmd_stop(args)
    return code or cmd_start(args)


def cmd_diagnose(args):
    command = [sys.executable, str(_LAUNCHER), "--diagnose"]
    if getattr(args, "data_dir", None):
        command.extend(["--data-dir", args.data_dir])
    if getattr(args, "read_only", False):
        command.append("--read-only")
    return subprocess.call(command, env=dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8"))


def cmd_schema(args):
    return cmd_api(argparse.Namespace(
        path="/api/v1/authoring/schema?" + urllib.parse.urlencode({"kind": args.kind}),
        method="GET", data=None, data_file=None, timeout=args.timeout, url=getattr(args, "url", None)))


def cmd_request(args):
    """Transport an external AI's work; never generate, approve or adopt content."""
    operation = args.operation
    path = "/api/v1/authoring/requests"
    if operation not in ("create", "list"):
        path += "/" + urllib.parse.quote(args.request_id, safe="")
    if operation == "feedback":
        path += "/feedback"
    elif operation == "submit":
        path += "/result"
    if operation in ("list", "get") and args.client_profile_id:
        path += "?" + urllib.parse.urlencode({"client_profile_id": args.client_profile_id})
    return cmd_api(argparse.Namespace(
        path=path, method="GET" if operation in ("list", "get") else "POST",
        data=None, data_file=getattr(args, "data_file", None), timeout=args.timeout,
        request_link=True, url=getattr(args, "url", None)))


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Rule Studio 的 Agent 快捷入口")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("start", help="启动并等待就绪")
    p.add_argument("--timeout", type=float, default=30.0, help="等待就绪的秒数")
    p.add_argument("--idle-seconds", type=int, default=1800, help="空闲自动退出秒数")
    p.add_argument("--data-dir")
    p.add_argument("--read-only", action="store_true")
    p.add_argument("--open-browser", action="store_true")
    p.set_defaults(fn=cmd_start)

    p = sub.add_parser("api", help="调用 API（内部换取 token）")
    p.add_argument("path")
    p.add_argument(
        "--method", default="GET",
        choices=("GET", "POST", "PUT", "PATCH", "DELETE"),
    )
    body = p.add_mutually_exclusive_group()
    body.add_argument("--data", default=None, help="JSON 字符串")
    body.add_argument("--data-file", help="读取 UTF-8 JSON 文件；- 表示标准输入")
    p.add_argument("--url", help="明确连接已打开工作台的回环 HTTP 地址，不另开实例")
    p.add_argument("--timeout", type=float, default=20.0)
    p.set_defaults(fn=cmd_api)

    p = sub.add_parser("schema", help="读取候选规则的真实字段说明与受控选项")
    p.add_argument("kind", choices=("domain_principle", "principle", "clause_module", "client_rule"))
    p.add_argument("--timeout", type=float, default=20.0)
    p.add_argument("--url", help="明确连接已打开工作台的回环 HTTP 地址")
    p.set_defaults(fn=cmd_schema)

    p = sub.add_parser("request", help="读取、提交或反馈 AI 整理请求；不自动审核或启用")
    operations = p.add_subparsers(dest="operation", required=True)
    for operation, help_text in (
        ("create", "保存自然语言要求与来源材料，等待外部 AI 整理"),
        ("list", "列出公共或明确选中客户的整理请求"),
        ("get", "读取当前要求、材料、反馈、版本和缺项"),
        ("submit", "提交外部 AI 已整理的候选供 Studio 核对"),
        ("feedback", "保存真实核对意见，供下一轮整理读取"),
    ):
        request_parser = operations.add_parser(operation, help=help_text)
        if operation not in ("create", "list"):
            request_parser.add_argument("request_id")
        if operation in ("get", "list"):
            request_parser.add_argument("--client-profile-id", help="仅在用户已明确选择该客户时提供")
        else:
            request_parser.add_argument("--data-file", required=True,
                                        help="UTF-8 JSON 请求文件；- 表示标准输入。更新须保留读取的版本与输入哈希")
        request_parser.add_argument("--timeout", type=float, default=20.0)
        request_parser.add_argument("--url", help="明确连接已打开工作台的回环 HTTP 地址")
        request_parser.set_defaults(fn=cmd_request)

    sub.add_parser("status", help="查看状态").set_defaults(fn=cmd_status)
    sub.add_parser("stop", help="停止本脚本启动的实例").set_defaults(fn=cmd_stop)

    p = sub.add_parser("restart", help="安全停止并按当前参数重启")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--idle-seconds", type=int, default=1800)
    p.add_argument("--data-dir")
    p.add_argument("--read-only", action="store_true")
    p.add_argument("--open-browser", action="store_true")
    p.set_defaults(fn=cmd_restart)
    p = sub.add_parser("diagnose", help="诊断环境，不启动服务")
    p.add_argument("--data-dir")
    p.add_argument("--read-only", action="store_true")
    p.set_defaults(fn=cmd_diagnose)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
