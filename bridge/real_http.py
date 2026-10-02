"""Loopback-only HTTP transport for the real-data chat UI."""
from __future__ import annotations

import json
import hmac
import mimetypes
import os
import re
import threading
import time
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from backend_contracts import AccountChangedError, ForecastRequestError, ROOT
from backend_service import Backend
from analysis_control import AnalysisInterrupted
from wechat_source import WeChatSource
from account_store import AccountConflict, AccountNotFound
from instance_identity import default_port, instance_id
from model_source import ModelSourceUnavailable
from product_profile import current_product

CHATUI = ROOT / "chatui"
PRODUCT = current_product()
CONTROL_TOKEN_ENV = PRODUCT.control_token_env
CONTROL_TOKEN_HEADER = PRODUCT.control_token_header


def app_version():
    try:
        value = json.loads((ROOT / "package.json").read_text(encoding="utf-8")).get("version")
        return value if isinstance(value, str) and value else None
    except (OSError, ValueError, AttributeError):
        return None


APP_VERSION = app_version()
JAVASCRIPT_SUFFIXES = {".js", ".mjs"}
JAVASCRIPT_MIME = "text/javascript; charset=utf-8"


def static_content_type(name):
    """Return a stable MIME type for files served to the browser.

    On Windows, ``mimetypes.guess_type`` can inherit a registry mapping that
    incorrectly reports JavaScript as ``text/plain``.  With ``nosniff`` that
    prevents the browser from executing the application bundle, so script
    types must be selected independently of the host registry.
    """
    if Path(name).suffix.lower() in JAVASCRIPT_SUFFIXES:
        return JAVASCRIPT_MIME
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def integer(value, default, maximum):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isdigit():
        raise ValueError("invalid limit")
    result = int(value)
    if not 1 <= result <= maximum:
        raise ValueError("limit out of range")
    return result


def user_value(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or any(ord(char) < 32 for char in value):
        raise ValueError("invalid user")
    return value


def request_id_value(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or any(ord(char) < 32 for char in value):
        raise ValueError("invalid requestId")
    return value


def make_handler(backend, accounts=None, control_token=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def trusted_request(self):
            port = self.server.server_port
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            origins = {f"http://{host}" for host in hosts}
            origin = self.headers.get("Origin")
            return (self.headers.get("Host") in hosts and
                    (origin is None or origin in origins) and
                    self.path.startswith("/") and not self.path.startswith("//"))

        def send(self, status, body, content_type="application/json; charset=utf-8"):
            payload = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # Switching chats cancels obsolete requests. There is no client left to
                # receive a second 503 response; retain the successfully read result.
                self.close_connection = True

        def query(self, path):
            return {key: values[0] for key, values in parse_qs(path.query, keep_blank_values=True).items()}

        def reject_post(self):
            # A Windows socket closed with unread incoming bytes can reset before
            # the client receives its 403. Send the denial first, then discard a
            # bounded body without parsing it or dispatching any backend work.
            self.send(403, {"error": "forbidden"})
            self.close_connection = True
            try:
                length = integer(self.headers.get("Content-Length"), 0, 65536)
            except ValueError:
                return
            if length is None or self.headers.get("Transfer-Encoding"):
                return
            deadline = time.monotonic() + .1
            original_timeout = self.connection.gettimeout()
            try:
                self.wfile.flush()
                while length:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self.connection.settimeout(remaining)
                    chunk = self.rfile.read1(min(length, 8192))
                    if not chunk:
                        break
                    length -= len(chunk)
            except OSError:
                pass
            finally:
                self.connection.settimeout(original_timeout)

        def do_GET(self):
            lease = getattr(backend, "request_lease", None)
            reader_scope = getattr(getattr(backend, "source", None), "request_scope", None)
            try:
                with lease() if callable(lease) else nullcontext():
                    with reader_scope() if callable(reader_scope) else nullcontext():
                        return self._do_GET()
            except RuntimeError:
                return self.send(503, {"error": "bridge-closing"})

        def _do_GET(self):
            if not self.trusted_request():
                return self.send(403, {"error": "forbidden"})
            parsed = urlsplit(self.path)
            try:
                query = self.query(parsed)
                if parsed.path == "/api/health":
                    result = {**backend.health(), "instanceId": instance_id(ROOT),
                              "appVersion": APP_VERSION}
                    if callable(getattr(accounts, "source_status", None)):
                        result["source"] = accounts.source_status()
                    return self.send(200, result)
                if parsed.path == "/product-config.js":
                    config = {"key": PRODUCT.key, "name": PRODUCT.product_name}
                    return self.send(200, ("window.ProductConfig = Object.freeze(" +
                        json.dumps(config, ensure_ascii=True) + ");\n").encode("utf-8"), JAVASCRIPT_MIME)
                if parsed.path == "/api/runtime":
                    return self.send(200, backend.runtime())
                if parsed.path == "/api/local-model":
                    return self.send(200, backend.local_model_status())
                if parsed.path == "/api/model-source":
                    try:
                        return self.send(200, backend.model_source())
                    except Exception:
                        return self.send(503, {"error": "model source unavailable"})
                if parsed.path == "/api/model-insights":
                    raw_ids = query.get("ids")
                    ids = None
                    if raw_ids is not None:
                        ids = json.loads(raw_ids)
                        if not isinstance(ids, list):
                            raise ValueError("invalid API insight ids")
                    if "around" in query:
                        return self.send(200, backend.model_insights(user_value(query.get("user")), ids, around=query["around"]))
                    return self.send(200, backend.model_insights(user_value(query.get("user")), ids))
                if parsed.path == "/api/model-portrait":
                    member = query.get("member")
                    return self.send(200, backend.model_portrait(user_value(query.get("user")),
                                                                 user_value(member) if member else None))
                if parsed.path=='/api/qq/analysis/control':
                    return self.send(200,backend.analysis_control_status(user_value(query.get('account')),user_value(query.get('user'))))
                if parsed.path == "/api/analysis-cache":
                    return self.send(200, backend.analysis_cache_status())
                if parsed.path == "/api/sessions":
                    data = backend.source.sessions()
                    if hasattr(accounts,'set_conversation_reading'):
                        paused=backend.selection_store.paused_reading(data['account'])
                        data['sessions']=[{**row,'readEnabled':row['username'] not in paused} for row in data.get('sessions',[])]
                    if accounts is not None:
                        accounts.observe(data)
                    return self.send(200, data)
                if parsed.path == "/api/conversation-selection":
                    return self.send(200, backend.conversation_selection())
                if parsed.path == "/api/accounts" and accounts is not None:
                    return self.send(200, accounts.list())
                if parsed.path == "/api/qq/import" and hasattr(accounts, "imports"):
                    return self.send(200, accounts.imports.status(query.get("jobId")))
                if parsed.path == "/api/qq/diagnostics" and hasattr(accounts, "imports"):
                    from qq_support import diagnostics
                    return self.send(200, diagnostics(backend, accounts, APP_VERSION))
                if parsed.path=='/api/qq/data/restore-status' and hasattr(accounts,'restore_status'):
                    return self.send(200,accounts.restore_status())
                if parsed.path == "/api/qq/data-scope" and hasattr(accounts, "imports"):
                    try:
                        scope=backend.source.data_scope(user_value(query.get("account")),user_value(query.get("user")))
                        if hasattr(accounts,'set_conversation_reading'):
                            scope['readEnabled']=scope['user'] not in backend.selection_store.paused_reading(scope['account'])
                        return self.send(200,scope)
                    except AccountChangedError:
                        return self.send(409, {"error": "account-changed"})
                    except ValueError:
                        return self.send(400, {"error": "invalid-local-scope"})
                    except Exception:
                        return self.send(503, {"error": "local-scope-unavailable"})
                if parsed.path in ("/api/qq/ingest-history", "/api/qq/message-provenance") and hasattr(accounts, "imports"):
                    try:
                        cursor = query.get("cursor") if parsed.path.endswith("message-provenance") else None
                        if parsed.path.endswith("message-provenance") and (not isinstance(cursor, str) or not cursor):
                            raise ValueError("message-cursor-required")
                        return self.send(200, backend.source.ingest_history(
                            user_value(query.get("account")), user_value(query.get("user")),
                            before=integer(query.get("before"), None, 9007199254740991),
                            limit=integer(query.get("limit"), 20, 50), message_cursor=cursor, kind=query.get("kind")))
                    except AccountChangedError:
                        return self.send(409, {"error": "account-changed"})
                    except ValueError:
                        return self.send(400, {"error": "invalid-ingest-history"})
                    except Exception:
                        return self.send(503, {"error": "ingest-history-unavailable"})
                if parsed.path == "/api/qq/connection" and getattr(accounts, "sync_manager", None) is not None:
                    return self.send(200, accounts.sync_manager.public())
                if parsed.path == "/api/qq/contacts" and getattr(accounts, "sync_manager", None) is not None:
                    from qq_connector import ConnectorError
                    try:
                        return self.send(200, accounts.sync_manager.contacts(integer(query.get("page"), 1, 100)))
                    except ConnectorError as error:
                        return self.send(409, {"error": error.code})
                if parsed.path=='/api/qq/members' and getattr(backend.source,'kind',None)=='qq':
                    expected=user_value(query.get('account'))
                    if backend.source.identity()[0]!=expected:
                        raise AccountChangedError()
                    user=user_value(query.get('user'))
                    with backend.source.read_library(expected):
                        return self.send(200,{'account':expected,'user':user,'members':backend.source.members(user)})
                if parsed.path == "/api/messages":
                    return self.send(200, backend.messages(user_value(query.get("user")), integer(query.get("limit"), 80, 500)))
                if parsed.path == "/api/history":
                    return self.send(200, backend.history(
                        user_value(query.get("account")), user_value(query.get("user")),
                        before=query.get("before"), around=query.get("around"),
                        limit=integer(query.get("limit"), 80, 200),member=query.get('member') or None))
                if parsed.path == "/api/history/search":
                    return self.send(200, backend.history_search(
                        user_value(query.get("account")), user_value(query.get("user")),
                        query=query.get("q"), day=query.get("date"),
                        before=query.get("before"), limit=integer(query.get("limit"), 50, 100),member=query.get('member') or None))
                if parsed.path == "/api/analysis":
                    return self.send(200, backend.analysis(user_value(query.get("user")),integer(query.get('limit'),80,80)))
                if parsed.path == "/api/profile":
                    member = query.get("member")
                    return self.send(200, backend.profile(user_value(query.get("user")),
                        user_value(member) if member else None, retry=query.get("retry") == "1"))
                if parsed.path == "/api/media":
                    image = backend.source.media(user_value(query.get("user")), user_value(query.get("id")))
                    return self.send(200, image[0], image[1]) if image else self.send(404, {
                        "error": "media unavailable",
                        "reason": getattr(getattr(backend.source, "media_reason", None), "value", None)})
                if parsed.path.startswith("/api/"):
                    return self.send(404, {"error": "not found"})
                target = (CHATUI / (parsed.path.lstrip("/") or "index.html")).resolve()
                if not target.is_relative_to(CHATUI.resolve()) or not target.is_file():
                    return self.send(404, {"error": "not found"})
                mime = static_content_type(target.name)
                payload = target.read_bytes()
                if target.name == "index.html" and PRODUCT.key == "qq":
                    html = payload.decode("utf-8")
                    for old, new in (("<title>WechatVibe</title>", "<title>QQVibe</title>"),
                                     ("/assets/wechatvibe-icon.png", "/assets/" + Path(PRODUCT.icon_png).name),
                                     ("<strong>WechatVibe</strong>", "<strong>QQVibe</strong>"),
                                     ("关于 WechatVibe", "关于 QQVibe"),
                                     ("连接当前微信账号", "打开本地 QQ 账号"),
                                     ("正在连接当前微信账号…", "正在读取 QQ 本地账号…"),
                                     ("正在连接微信…", "正在读取 QQ 本地账号…"),
                                     ("已保存的微信账号", "已保存的 QQ 账号"),
                                     ("不会发送到微信", "不会发送到 QQ")):
                        html = html.replace(old, new)
                    payload = html.encode("utf-8")
                return self.send(200, payload, mime)
            except ValueError as exc:
                return self.send(400, {"error": str(exc)})
            except Exception as exc:
                return self.send(503, {"error": type(exc).__name__, "message": str(exc)[:200]})

        def do_POST(self):
            lease = getattr(backend, "request_lease", None)
            reader_scope = getattr(getattr(backend, "source", None), "request_scope", None)
            try:
                with lease() if callable(lease) else nullcontext():
                    # Account activation intentionally replaces the read binding. Other
                    # endpoints must still detect an accidental mid-request switch.
                    switching = urlsplit(self.path).path in ("/api/accounts/activate", "/api/qq/connection/connect", "/api/qq/connection/connect-standard")
                    with reader_scope() if callable(reader_scope) and not switching else nullcontext():
                        return self._do_POST()
            except RuntimeError:
                return self.send(503, {"error": "bridge-closing"})

        def _do_POST(self):
            if not self.trusted_request():
                return self.reject_post()
            endpoint = urlsplit(self.path).path
            if endpoint == "/api/control/shutdown":
                supplied = self.headers.get(CONTROL_TOKEN_HEADER, "")
                if (self.client_address[0] != "127.0.0.1" or
                        not isinstance(control_token, str) or
                        re.fullmatch(r"[0-9a-f]{64}", control_token) is None or
                        not hmac.compare_digest(supplied, control_token)):
                    return self.send(403, {"error": "forbidden"})
                if self.path != endpoint or self.headers.get("Content-Length") != "0":
                    return self.send(400, {"error": "invalid control request"})
                self.send(202, {"stopping": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            model_endpoints = ("/api/model-source/list", "/api/model-source/test",
                               "/api/model-source/activate", "/api/model-source/clear-key")
            if endpoint not in ("/api/analyze", "/api/predict-reply", "/api/messages/batch",
                                 "/api/runtime", "/api/local-model", "/api/model-insights",
                                 "/api/model-portrait", "/api/analysis-cache/clear",
                                 "/api/analysis-cache/resume", "/api/conversation-selection",
                                 "/api/accounts/activate",
                                 "/api/qq/import/preview", "/api/qq/import/identity",
                                 "/api/qq/import/commit", "/api/qq/import/cancel",
                                 "/api/qq/connection/configure", "/api/qq/connection/connect",
                                 "/api/qq/connection/connect-standard",
                                 "/api/qq/connection/disconnect", "/api/qq/connection/clear-token",
                                 "/api/qq/connection/retry",
                                 "/api/qq/connection/add-contact",
                                 "/api/qq/connection/add-group",
                                 "/api/qq/analysis/control", "/api/qq/analysis/budget",
                                 "/api/qq/conversation/read", "/api/qq/conversation/clear",
                                 "/api/qq/data/backup", "/api/qq/data/restore-preview",
                                 *model_endpoints):
                return self.send(404, {"error": "not found"})
            content_type = [part.strip().lower() for part in self.headers.get("Content-Type", "").split(";")]
            if content_type[0] != "application/json" or any(part != "charset=utf-8" for part in content_type[1:]):
                return self.send(415, {"error": "application/json required"})
            echo = {}
            try:
                length = integer(self.headers.get("Content-Length"), None, 65536)
                if length is None:
                    raise ValueError("body required")
                request = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(request, dict) or "texts" in request:
                    raise ValueError("invalid request")
                if endpoint=='/api/qq/analysis/budget':
                    if set(request)!={'account','user','requests'}:raise ValueError('invalid-request-budget')
                    try:
                        return self.send(200,backend.grant_analysis_budget(user_value(request['account']),user_value(request['user']),request['requests']))
                    except AccountChangedError:return self.send(409,{'error':'account-changed'})
                    except ValueError:return self.send(400,{'error':'invalid-request-budget'})
                if endpoint=='/api/qq/analysis/control':
                    if set(request)!={'account','user','action'}:raise ValueError('invalid-analysis-control')
                    try:
                        return self.send(200,backend.control_analysis(user_value(request['account']),user_value(request['user']),request['action']))
                    except AccountChangedError:return self.send(409,{'error':'account-changed'})
                    except ValueError:return self.send(400,{'error':'invalid-analysis-control'})
                if endpoint in ('/api/qq/conversation/read','/api/qq/conversation/clear') and hasattr(accounts,'clear_conversation'):
                    fields={'account','user','enabled'} if endpoint.endswith('/read') else {'account','user','confirm'}
                    if set(request)!=fields or (endpoint.endswith('/clear') and request['confirm'] is not True):
                        raise ValueError('invalid-conversation-operation')
                    try:
                        if endpoint.endswith('/read'):
                            return self.send(200,accounts.set_conversation_reading(user_value(request['account']),user_value(request['user']),request['enabled']))
                        return self.send(200,accounts.clear_conversation(user_value(request['account']),user_value(request['user'])))
                    except AccountChangedError:return self.send(409,{'error':'account-changed'})
                    except ValueError:return self.send(400,{'error':'invalid-conversation-operation'})
                    except Exception:return self.send(503,{'error':'conversation-operation-unavailable'})
                if endpoint in ('/api/qq/data/backup','/api/qq/data/restore-preview') and hasattr(accounts,'backup_data'):
                    try:
                        if endpoint.endswith('/backup'):
                            if set(request) not in ({'path'},{'path','uiPreferences'}):raise ValueError('invalid-backup-request')
                            return self.send(200,accounts.backup_data(user_value(request['path']),request.get('uiPreferences')))
                        if set(request)!={'path'}:raise ValueError('invalid-backup-request')
                        return self.send(200,accounts.preview_restore(user_value(request['path'])))
                    except (ValueError,FileExistsError,KeyError):
                        return self.send(400,{'error':'data-backup-invalid'})
                    except Exception:
                        return self.send(503,{'error':'data-backup-unavailable'})
                if endpoint.startswith("/api/qq/connection/"):
                    coordinator = getattr(accounts, "sync_manager", None)
                    if coordinator is None:
                        return self.send(404, {"error": "connector-unavailable"})
                    from qq_connector import ConnectorError, failure_code
                    try:
                        action = endpoint.rsplit("/", 1)[1]
                        if action == "connect-standard":
                            if request:
                                raise ValueError("invalid-connection-request")
                            result = coordinator.connect_standard()
                        elif action == "configure":
                            if set(request) not in ({"baseUrl", "ownerUin"}, {"baseUrl", "ownerUin", "token"}):
                                raise ValueError("invalid-connection-request")
                            result = coordinator.configure(request["baseUrl"], request["ownerUin"], request.get("token"))
                        elif action == "retry":
                            if set(request) not in ({"account", "user"}, {"account", "user", "kind"}):
                                raise ValueError("invalid-connection-request")
                            result = coordinator.retry(user_value(request["account"]), user_value(request["user"]), request.get("kind", "tail"))
                        elif action == "add-contact":
                            if set(request) != {"peerUin", "name"}:
                                raise ValueError("invalid-contact-request")
                            return self.send(200, coordinator.add_contact(request["peerUin"], request["name"]))
                        elif action == 'add-group':
                            if set(request)!={'groupCode'}:
                                raise ValueError('invalid-group-request')
                            return self.send(200,coordinator.add_group(request['groupCode']))
                        elif action=='connect':
                            if set(request) not in (set(),{'allowUnverified'}) or ('allowUnverified' in request and request['allowUnverified'] is not True):
                                raise ValueError('invalid-version-consent')
                            result=coordinator.connect(allow_unverified=request.get('allowUnverified',False))
                        else:
                            if request or action not in ('disconnect','clear-token'):
                                raise ValueError("invalid-connection-request")
                            result = coordinator.disconnect(clear_token=action == "clear-token")
                        return self.send(200, result)
                    except ConnectorError as error:
                        return self.send(409, {"error": error.code})
                    except ValueError:
                        return self.send(400, {"error": "invalid-connection-request"})
                    except Exception as error:
                        return self.send(503, {"error": failure_code(error)})
                if endpoint.startswith("/api/qq/import/"):
                    if not hasattr(accounts, "imports"):
                        return self.send(404, {"error": "import-unavailable"})
                    imports = accounts.imports
                    action = endpoint.rsplit("/", 1)[1]
                    fields = {"preview": ({"path"}, {"path", "ownerUin"}),
                              "identity": ({"jobId", "ownerUin"}, {"jobId", "ownerUin", "peerUid"}),
                              "commit": ({"jobId", "previewToken", "acceptPartial"},),
                              "cancel": ({"jobId"},)}
                    if set(request) not in fields[action]:
                        raise ValueError("invalid-import-request")
                    if action == "preview":
                        result = imports.start(request["path"], request.get("ownerUin"))
                    elif action == "identity":
                        result = imports.map_owner(request["jobId"], request["ownerUin"], request.get("peerUid"))
                    elif action == "commit":
                        result = imports.commit(request["jobId"], request["previewToken"], request["acceptPartial"])
                    else:
                        result = imports.cancel(request["jobId"])
                    return self.send(202, result)
                if endpoint == "/api/accounts/activate":
                    if accounts is None or not callable(getattr(accounts, "activate", None)):
                        return self.send(404, {"error": "account-activation-unavailable"})
                    identifier = request.get("accountId")
                    if set(request) != {"accountId"} or not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{64}", identifier):
                        raise ValueError("invalid account activation")
                    try:
                        return self.send(200, accounts.activate(identifier))
                    except AccountConflict as exc:
                        return self.send(409, {"error": "account-busy", "message": str(exc)})
                    except AccountNotFound:
                        return self.send(404, {"error": "account-not-found"})
                if endpoint in model_endpoints:
                    try:
                        if endpoint == "/api/model-source/list":
                            return self.send(200, backend.model_source_list(request))
                        if endpoint == "/api/model-source/test":
                            return self.send(200, backend.model_source_test(request))
                        if endpoint == "/api/model-source/activate":
                            return self.send(200, backend.model_source_activate(request))
                        return self.send(200, backend.model_source_clear_key(request))
                    except ValueError:
                        return self.send(400, {"error": "invalid model source request"})
                    except ModelSourceUnavailable as exc:
                        return self.send(503, {"error": str(exc)})
                    except Exception:
                        return self.send(503, {"error": "model source unavailable"})
                if endpoint == "/api/conversation-selection":
                    if set(request) != {"expectedAccount", "session", "selected"} or type(request["selected"]) is not bool:
                        raise ValueError("invalid conversation selection")
                    return self.send(200, backend.set_conversation_selected(
                        user_value(request["expectedAccount"]), user_value(request["session"]),
                        request["selected"]))
                if endpoint == "/api/runtime":
                    if set(request) != {"provider"} or request["provider"] not in ("cpu", "gpu"):
                        raise ValueError("invalid provider")
                    return self.send(200, backend.configure_runtime(request["provider"]))
                if endpoint == "/api/local-model":
                    value = request.get("path")
                    if set(request) != {"path"} or not isinstance(value, str) or not 1 <= len(value) <= 4096 or any(ord(char) < 32 for char in value):
                        raise ValueError("invalid model path")
                    return self.send(200, backend.configure_local_model(value))
                if endpoint == "/api/model-insights":
                    account = user_value(request.get("account"))
                    user = user_value(request.get("user"))
                    # API insight experiments send the bounded sample as one provider
                    # request. Keep this transport cap aligned with the analyzer's
                    # per-request target cap instead of silently forcing two-target
                    # batches.
                    limit = integer(request.get("limit"), 500, 500)
                    around = request.get("around")
                    return self.send(202, backend.start_model_insights(
                        account, user, limit, request.get("targetIds"), around))
                if endpoint == "/api/model-portrait":
                    if set(request) not in ({"account", "user"},
                                            {"account", "user", "member"},
                                            {"account", "user", "refreshAxes"},
                                            {"account", "user", "member", "refreshAxes"}):
                        raise ValueError("invalid portrait request")
                    member = request.get("member")
                    refresh_axes = request.get("refreshAxes", False)
                    if type(refresh_axes) is not bool:
                        raise ValueError("invalid portrait request")
                    return self.send(202, backend.start_model_portrait(
                        user_value(request.get("account")), user_value(request.get("user")),
                        user_value(member) if member is not None else None,
                        refresh_axes=refresh_axes))
                if endpoint in ("/api/analysis-cache/clear", "/api/analysis-cache/resume"):
                    if set(request) != {"account", "sourceId"}:
                        raise ValueError("invalid cache request")
                    account = user_value(request.get("account"))
                    source_id = user_value(request.get("sourceId"))
                    result = (backend.analysis_cache_clear(account, source_id)
                              if endpoint.endswith("/clear") else
                              backend.analysis_cache_resume(account, source_id))
                    return self.send(200, result)
                if endpoint == "/api/messages/batch":
                    account = user_value(request.get("account"))
                    users = request.get("users")
                    if not isinstance(users, list) or not 1 <= len(users) <= 64:
                        raise ValueError("invalid users")
                    users = [user_value(user) for user in users]
                    if len(set(users)) != len(users):
                        raise ValueError("duplicate users")
                    return self.send(200, backend.message_windows(account, users))
                if endpoint == "/api/predict-reply":
                    echo = {key: request[key] for key in ("user", "account", "requestId")
                            if isinstance(request.get(key), str)}
                    user = user_value(request.get("user"))
                    account = user_value(request.get("account"))
                    request_id = request_id_value(request.get("requestId"))
                    draft = request.get("draft", "")
                    if not isinstance(draft, str) or len(draft) > 2000:
                        raise ValueError("invalid draft")
                    expected = request.get("expectedLastMessageId")
                    if expected is not None:
                        expected = user_value(expected)
                    member = request.get("member")
                    if member is not None:
                        member = user_value(member)
                    return self.send(200, backend.predict_reply(user, account, request_id, draft, expected, member))
                user = user_value(request.get("user"))
                expected_account = user_value(request.get("account"))
                mode = request.get("mode")
                if mode not in ("recent", "history", "incremental"):
                    raise ValueError("invalid mode")
                limit = (None if mode == "incremental" else
                         "all" if mode == "history" and request.get("limit") == "all" else
                         integer(request.get("limit"), 80 if mode == "recent" else 500,
                                 80 if mode == "recent" else 5000))
                return self.send(202, {"job": backend.start(user, mode, limit,
                                                             expected_account=expected_account)})
            except AnalysisInterrupted as exc:
                return self.send(409,{"error":"analysis-"+exc.state,"analysisControl":exc.state})
            except ForecastRequestError as exc:
                return self.send(exc.status, {**echo, "error": exc.code, "message": exc.message})
            except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
                return self.send(400, {**echo, "error": str(exc)})
            except Exception as exc:
                return self.send(503, {**echo, "error": type(exc).__name__, "message": str(exc)[:200]})

        def do_DELETE(self):
            if not self.trusted_request():
                return self.send(403, {"error": "forbidden"})
            path = urlsplit(self.path).path
            prefix = "/api/accounts/"
            if accounts is None or not path.startswith(prefix):
                return self.send(404, {"error": "not found"})
            try:
                result = accounts.delete(path[len(prefix):])
                try:
                    return self.send(200, result)
                finally:
                    if result.get("exitApp"):
                        threading.Thread(target=self.server.shutdown, daemon=True).start()
            except AccountConflict as exc:
                return self.send(409, {"error": "account-busy", "message": str(exc)})
            except AccountNotFound as exc:
                return self.send(404, {"error": "account-not-found", "message": str(exc)})
            except ValueError:
                return self.send(400, {"error": "invalid-account"})
            except OSError:
                return self.send(409, {"error": "account-busy", "message": "账号缓存正在使用，请稍后重试"})
            except Exception:
                return self.send(503, {"error": "account-unavailable", "message": "暂时无法清理账号数据，请稍后重试"})

    return Handler


def build_source(classifier, profile=PRODUCT):
    """QQ mode never constructs WeChatSource, so it never scans WeChat accounts or keys."""
    if profile.key == "qq":
        from qq_source import QQSource
        return QQSource(profile=profile)
    return WeChatSource(classifier=classifier)


def build_account_api(backend, profile=PRODUCT, root=ROOT, *, live_connector_validated=False):
    if profile.key == "qq":
        from qq_account_api import QQAccountAPI
        from qq_sync import QQSync
        accounts = QQAccountAPI(backend, profile.data_root(root))
        def schedule_analysis(account, user):
            if getattr(backend, "active_model_source_mode", "local") == "local" and not backend.closing:
                if backend.source.conversation_kind(user)=='group':
                    return
                backend.start(user, "incremental", None, expected_account=account)
        accounts.sync_manager = QQSync(accounts, live_validated=live_connector_validated, on_commit=schedule_analysis,
                                      initial_read=live_connector_validated)
        backend.qq_sync = accounts.sync_manager
        return accounts
    from account_api import AccountAPI
    return AccountAPI(backend, profile.state_dir("real-client-data", root))


def main(classifier):
    control_token = os.environ.pop(PRODUCT.control_token_env, None)
    backend = Backend(build_source(classifier))
    port = integer(os.environ.get("CHATUI_PORT"), default_port(ROOT), 65535)
    # Explicit local core-preview launch; this does not certify the full G1/G3
    # acceptance plan. The preparation step selects one real single chat.
    core_preview = PRODUCT.key == "qq" and os.environ.get("QQVIBE_LIVE_CORE") == "1"
    accounts = build_account_api(backend, live_connector_validated=core_preview)
    if core_preview:
        # The first user milestone is the selected real chat, not a full-history
        # sweep. Forward sync continues; backfill/reconciliation come later.
        accounts.sync_manager.background_work_enabled = False
        if accounts.sync_manager.public()["enabled"]:
            try:
                accounts.sync_manager.connect()
            except Exception:
                # Startup remains usable offline; connect() records the error.
                pass
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(backend, accounts, control_token))
    print(f"chatui server on http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if hasattr(accounts, "imports"):
            accounts.imports.close()
        backend.shutdown()
    return 0
