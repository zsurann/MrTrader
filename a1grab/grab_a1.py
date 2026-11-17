#!/usr/bin/env python3
"""Retry Always Free A1.Flex (2 OCPU / 12 GB) in eu-milan-1 until it lands.

PAPER-only desk context: this script only creates the VM. It never migrates
the desk, never upgrades the tenancy to Pay As You Go, and never launches a
shape or size outside the Always Free cap hardcoded below.

Credentials live in /etc/memedesk/oci/ (mode 600) and Telegram settings are
read from /etc/memedesk/memedesk.env. Nothing secret is printed or logged.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from email.utils import formatdate
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ---------------------------------------------------------------------------
# Hard caps: Always Free A1 in the home region is 4 OCPU / 24 GB. We request
# the 2 / 12 size the user asked for, and refuse to run if config tries more.
# Boot volume stays at the 50 GB default. Never touch these without an explicit
# review that the result would still be free.
# ---------------------------------------------------------------------------
SHAPE = "VM.Standard.A1.Flex"
OCPUS = 2
MEMORY_GB = 12
BOOT_GB = 50
DISPLAY_NAME = "memedesk-a1"
HOSTNAME_LABEL = "memedesk-a1"
REGION = "eu-milan-1"
HOME_REGION = "eu-milan-1"
API_VERSION = "20160918"

OCI_DIR = Path(os.environ.get("A1GRAB_OCI_DIR", "/etc/memedesk/oci"))
OCI_CONFIG = OCI_DIR / "config"
LAUNCH_ENV = OCI_DIR / "launch.env"
SSH_PUB = OCI_DIR / "oci_memedesk.pub"
DESK_ENV = Path("/etc/memedesk/memedesk.env")
STATE_DIR = Path(os.environ.get("A1GRAB_STATE_DIR", "/var/lib/memedesk"))
STATE_PATH = STATE_DIR / "a1grab.state"
LOG_PATH = STATE_DIR / "a1grab.log"
UNIT_TIMER = "memedesk-a1grab.timer"

# Treat anything not TERMINATED as "already exists" so we never duplicate.
ACTIVE_STATES = {
    "PROVISIONING",
    "CREATING",
    "RUNNING",
    "STARTING",
    "STOPPING",
    "STOPPED",
    "RESETTING",
    "MOVING",
    "TERMINATING",
}

CAPACITY_MARKERS = (
    "out of host capacity",
    "out of capacity",
    "outofcapacity",
    "insufficient host capacity",
    "no host",
)
# Messages that may mean "this would cost money / needs a paid upgrade".
COST_MARKERS = (
    "pay as you go",
    "payg",
    "upgrade your account",
    "upgrade the account",
    "not eligible",
    "always free eligible",
    "paid resource",
    "convert your account",
    "credit card",
    "billing",
    "subscription has expired",
    "credits exhausted",
    "no credits",
)
LIMIT_MARKERS = (
    "limitexceeded",
    "service limit",
    "quota exceeded",
    "exceeded your quota",
    "exceeded the limit",
    "cannot create more",
)

MAX_BACKOFF_SEC = 2 * 60 * 60
# After an A1 exists, keep polling for a public IP this many times, then
# notify with the OCID anyway and stop.
IP_POLL_GIVE_UP = 6


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    line = f"{now()} {msg}"
    print(line, flush=True)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if LOG_PATH.stat().st_size > 200_000:
            lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
            LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def redact(text: str) -> str:
    """Strip tokens, PEM, and key-looking values from text before logging."""
    if not text:
        return ""
    out = text
    # bot tokens look like 123456:AA...
    import re

    out = re.sub(r"\d{8,}:[A-Za-z0-9_-]{20,}", "<redacted-token>", out)
    out = re.sub(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", "<pem>", out, flags=re.S)
    out = re.sub(r"(?i)(api[_-]?key|token|secret|password|authorization|signature)=([^\s\"&]+)", r"\1=<redacted>", out)
    return out[:800]


def load_ini(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    section = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def load_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".state.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(STATE_PATH)


def telegram(text: str, desk_env: dict) -> bool:
    token = (desk_env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (desk_env.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat:
        log("telegram skipped (token or chat id missing)")
        return False
    body = json.dumps({"chat_id": chat, "text": text}).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            ok = 200 <= resp.status < 300
        log("telegram sent" if ok else f"telegram http {resp.status}")
        return ok
    except Exception as e:
        log(f"telegram failed: {type(e).__name__}")
        return False


def disable_timer() -> None:
    """Stop further retries. Safe to call more than once."""
    try:
        rc = os.spawnlp(os.P_WAIT, "systemctl", "systemctl", "disable", "--now", UNIT_TIMER)
        log(f"timer disable --now {UNIT_TIMER} rc={rc}")
    except OSError as e:
        log(f"timer disable failed: {type(e).__name__}")


class Oci:
    def __init__(self, cfg: dict[str, str]):
        self.user = cfg["user"]
        self.fingerprint = cfg["fingerprint"]
        self.tenancy = cfg["tenancy"]
        self.region = cfg.get("region") or REGION
        if self.region != HOME_REGION:
            raise SystemExit(f"refusing to run: region is {self.region}, home Always Free region must be {HOME_REGION}")
        key_file = Path(cfg["key_file"])
        pem = key_file.read_bytes()
        self.key = serialization.load_pem_private_key(pem, password=None)
        self.key_id = f"{self.tenancy}/{self.user}/{self.fingerprint}"
        self.iaas = f"https://iaas.{self.region}.oraclecloud.com"

    def _headers(self, method: str, url: str, body: bytes) -> dict[str, str]:
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        if parsed.query:
            path = f"{path}?{parsed.query}"
        date = formatdate(usegmt=True)
        headers = {"date": date, "host": parsed.netloc}
        method_u = method.upper()
        if method_u in ("POST", "PUT", "PATCH") or body:
            headers["x-content-sha256"] = base64.b64encode(hashlib.sha256(body).digest()).decode()
            headers["content-type"] = "application/json"
            headers["content-length"] = str(len(body))
            signed = ["date", "(request-target)", "host", "content-length", "content-type", "x-content-sha256"]
        else:
            signed = ["date", "(request-target)", "host"]
        target = f"{method.lower()} {path}"
        lines = []
        for name in signed:
            if name == "(request-target)":
                lines.append(f"(request-target): {target}")
            else:
                lines.append(f"{name}: {headers[name]}")
        sig = self.key.sign("\n".join(lines).encode(), padding.PKCS1v15(), hashes.SHA256())
        headers["authorization"] = (
            f'Signature version="1",keyId="{self.key_id}",algorithm="rsa-sha256",'
            f'headers="{" ".join(signed)}",signature="{base64.b64encode(sig).decode()}"'
        )
        return headers

    def request(self, method: str, url: str, payload: dict | None = None) -> tuple[int, dict | list | str, str]:
        body = b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        headers = self._headers(method, url, body)
        req = urllib.request.Request(url, data=(body or None), method=method, headers=headers)
        req_id = ""
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                raw = resp.read()
                req_id = resp.headers.get("opc-request-id") or ""
                status = resp.status
        except urllib.error.HTTPError as e:
            raw = e.read() or b""
            req_id = e.headers.get("opc-request-id") if e.headers else ""
            status = e.code
        text = raw.decode("utf-8", errors="replace") if raw else ""
        parsed: dict | list | str
        try:
            parsed = json.loads(text) if text else {}
        except json.JSONDecodeError:
            parsed = text
        return status, parsed, req_id

    def get(self, path: str, params: dict[str, str] | None = None):
        url = self.iaas + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return self.request("GET", url)

    def post(self, path: str, payload: dict):
        return self.request("POST", self.iaas + path, payload)


def err_text(parsed: dict | list | str) -> str:
    if isinstance(parsed, dict):
        msg = parsed.get("message") or parsed.get("Message") or ""
        code = parsed.get("code") or parsed.get("Code") or ""
        return f"{code}: {msg}".strip(": ")
    return redact(str(parsed))


def is_capacity(status: int, parsed) -> bool:
    t = err_text(parsed).lower()
    return any(m in t for m in CAPACITY_MARKERS)


def is_cost_risk(status: int, parsed) -> bool:
    t = err_text(parsed).lower()
    return any(m in t for m in COST_MARKERS)


def is_limit(status: int, parsed) -> bool:
    t = err_text(parsed).lower()
    code = (parsed.get("code") if isinstance(parsed, dict) else "") or ""
    return code in {"LimitExceeded", "QuotaExceeded"} or any(m in t for m in LIMIT_MARKERS)


def is_rate(status: int, parsed) -> bool:
    if status == 429:
        return True
    t = err_text(parsed).lower()
    return "too many requests" in t or "too many request" in t


def list_instances(oci: Oci, compartment_id: str) -> list[dict]:
    items: list[dict] = []
    params = {"compartmentId": compartment_id, "limit": "100"}
    # One page is enough for this tenancy; follow a single next-page if present.
    for _ in range(5):
        status, parsed, req_id = oci.get(f"/{API_VERSION}/instances", params)
        if status != 200 or not isinstance(parsed, list):
            raise RuntimeError(f"list instances http {status} req={req_id} {redact(err_text(parsed))}")
        items.extend(parsed)
        # urllib doesn't expose response headers here; unpaged list is fine.
        break
    return items


def a1_instances(instances: list[dict]) -> list[dict]:
    found = []
    for inst in instances:
        shape = inst.get("shape") or ""
        state = (inst.get("lifecycleState") or inst.get("lifecycle-state") or "").upper()
        if shape == SHAPE and state in ACTIVE_STATES:
            found.append(inst)
    return found


def public_ip(oci: Oci, compartment_id: str, instance_id: str) -> str | None:
    status, parsed, req_id = oci.get(
        f"/{API_VERSION}/vnicAttachments",
        {"compartmentId": compartment_id, "instanceId": instance_id},
    )
    if status != 200 or not isinstance(parsed, list) or not parsed:
        log(f"vnicAttachments http {status} req={req_id}")
        return None
    vnic_id = parsed[0].get("vnicId") or parsed[0].get("vnic-id")
    if not vnic_id:
        return None
    status, vnic, req_id = oci.get(f"/{API_VERSION}/vnics/{vnic_id}")
    if status != 200 or not isinstance(vnic, dict):
        log(f"vnic http {status} req={req_id}")
        return None
    ip = vnic.get("publicIp") or vnic.get("public-ip")
    return ip or None


def finish_success(oci: Oci, launch: dict, inst: dict, desk_env: dict, state: dict) -> int:
    instance_id = inst.get("id") or ""
    state["instance_id"] = instance_id
    state["display_name"] = inst.get("displayName") or inst.get("display-name") or DISPLAY_NAME
    state["lifecycle_state"] = inst.get("lifecycleState") or inst.get("lifecycle-state")
    ip = public_ip(oci, launch["compartment_id"], instance_id) if instance_id else None
    if ip:
        state["public_ip"] = ip
        state["ip_polls"] = 0
        if not state.get("notified"):
            ok = telegram(
                "MemeDesk: istanza Always Free A1 (2 OCPU / 12 GB, Ubuntu, eu-milan-1) "
                f"pronta. Non ho spostato il desk. IP pubblico: {ip}\n"
                f"SSH: ssh -i ~/.ssh/oci_memedesk ubuntu@{ip}\n"
                "I tentativi automatici sono stati fermati. Per riattivarli: "
                "sudo systemctl enable --now memedesk-a1grab.timer",
                desk_env,
            )
            state["notified"] = bool(ok)
            state["notified_at"] = now()
        save_state(state)
        disable_timer()
        log(f"SUCCESS instance={instance_id} ip={ip}")
        return 0
    polls = int(state.get("ip_polls") or 0) + 1
    state["ip_polls"] = polls
    save_state(state)
    if polls >= IP_POLL_GIVE_UP:
        if not state.get("notified"):
            telegram(
                "MemeDesk: istanza Always Free A1 creata, ma l'IP pubblico non è ancora "
                f"visibile dopo diversi tentativi. OCID termina con: ...{instance_id[-12:]}. "
                "Controlla Compute → istanze (Milano). I tentativi automatici sono fermati.",
                desk_env,
            )
            state["notified"] = True
            state["notified_at"] = now()
            save_state(state)
        disable_timer()
        log(f"SUCCESS without public IP yet instance={instance_id} polls={polls}")
        return 0
    log(f"A1 exists instance={instance_id} state={state.get('lifecycle_state')} waiting for public IP poll={polls}")
    return 0


def launch_instance(oci: Oci, launch: dict) -> tuple[int, dict | list | str, str]:
    ssh = Path(launch["ssh_pub"]).read_text(encoding="utf-8").strip()
    payload = {
        "compartmentId": launch["compartment_id"],
        "availabilityDomain": launch["availability_domain"],
        "displayName": DISPLAY_NAME,
        "shape": SHAPE,
        "shapeConfig": {"ocpus": OCPUS, "memoryInGBs": MEMORY_GB},
        "sourceDetails": {
            "sourceType": "image",
            "imageId": launch["image_id"],
            "bootVolumeSizeInGBs": BOOT_GB,
        },
        "createVnicDetails": {
            "subnetId": launch["subnet_id"],
            "assignPublicIp": True,
            "assignPrivateDnsRecord": True,
            "hostnameLabel": HOSTNAME_LABEL,
        },
        "metadata": {"ssh_authorized_keys": ssh},
        "freeformTags": {"memedesk": "a1grab", "tier": "always-free"},
    }
    return oci.post(f"/{API_VERSION}/instances", payload)


def apply_backoff(state: dict, seconds: int) -> None:
    seconds = min(max(seconds, 60), MAX_BACKOFF_SEC)
    until = int(time.time()) + seconds
    state["backoff_until"] = until
    state["backoff_sec"] = seconds
    save_state(state)
    log(f"backoff {seconds}s until {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(until))}")


def main() -> int:
    if os.geteuid() != 0:
        # Still ok if the files are readable, but the timer runs as root.
        pass
    if not OCI_CONFIG.exists():
        log(f"missing {OCI_CONFIG}")
        return 2
    cfg = load_ini(OCI_CONFIG)
    for k in ("user", "fingerprint", "tenancy", "key_file"):
        if not cfg.get(k):
            log(f"oci config missing {k}")
            return 2
    extras = load_env_file(LAUNCH_ENV)
    # IDs come from launch.env; sizes are hardcoded and not overridable.
    launch = {
        "compartment_id": extras.get("COMPARTMENT_ID") or cfg.get("tenancy"),
        "availability_domain": extras.get("AVAILABILITY_DOMAIN"),
        "subnet_id": extras.get("SUBNET_ID"),
        "image_id": extras.get("IMAGE_ID"),
        "ssh_pub": extras.get("SSH_PUBKEY") or str(SSH_PUB),
    }
    # Safety: refuse any attempt to raise size via env.
    for banned, expected in (("OCPUS", OCPUS), ("MEMORY_GB", MEMORY_GB), ("BOOT_GB", BOOT_GB), ("SHAPE", SHAPE)):
        if banned in extras and str(extras[banned]) != str(expected):
            log(f"refusing to run: {banned} in launch.env is not the Always Free hard cap {expected}")
            return 3
    missing = [k for k, v in launch.items() if not v]
    if missing:
        log(f"launch.env missing {missing}")
        return 2
    if not Path(launch["ssh_pub"]).exists():
        log("missing ssh public key file")
        return 2

    desk_env = load_env_file(DESK_ENV)
    state = load_state()
    now_ts = int(time.time())
    if int(state.get("backoff_until") or 0) > now_ts:
        log(f"skipping (backoff until {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(state['backoff_until']))})")
        return 0
    if state.get("stop_reason"):
        log(f"stopped earlier: {state['stop_reason']}")
        disable_timer()
        return 0

    try:
        oci = Oci(cfg)
    except SystemExit as e:
        log(str(e))
        return 3
    except Exception as e:
        log(f"failed to load OCI key: {type(e).__name__}")
        return 2

    try:
        instances = list_instances(oci, launch["compartment_id"])
    except Exception as e:
        msg = redact(str(e))
        log(f"list failed: {msg}")
        if "429" in msg or "too many" in msg.lower():
            prev = int(state.get("backoff_sec") or 15 * 60)
            apply_backoff(state, min(prev * 2, MAX_BACKOFF_SEC) if state.get("backoff_sec") else 15 * 60)
            return 0
        return 1

    existing = a1_instances(instances)
    if existing:
        inst = existing[0]
        if len(existing) > 1:
            log(f"warning: {len(existing)} A1 instances already present; will not create another")
        return finish_success(oci, launch, inst, desk_env, state)

    log(f"no A1 present (other instances: {len(instances)}); launching {SHAPE} {OCPUS} OCPU {MEMORY_GB} GB boot {BOOT_GB} GB")
    try:
        status, parsed, req_id = launch_instance(oci, launch)
    except Exception as e:
        log(f"launch request failed: {type(e).__name__}: {redact(str(e))}")
        return 1

    if status in (200, 201) and isinstance(parsed, dict) and parsed.get("id"):
        inst = parsed
        log(f"launch accepted id=...{inst['id'][-12:]} state={inst.get('lifecycleState')} req={req_id}")
        state["instance_id"] = inst["id"]
        save_state(state)
        return finish_success(oci, launch, inst, desk_env, state)

    text = err_text(parsed)
    log(f"launch http {status} req={req_id} {redact(text)}")

    if is_rate(status, parsed):
        prev = int(state.get("backoff_sec") or 15 * 60)
        nxt = 15 * 60 if not state.get("backoff_sec") else min(prev * 2, MAX_BACKOFF_SEC)
        apply_backoff(state, nxt)
        return 0

    if is_capacity(status, parsed):
        state["last_capacity"] = now()
        state.pop("backoff_sec", None)
        state.pop("backoff_until", None)
        save_state(state)
        return 0

    if is_cost_risk(status, parsed) or is_limit(status, parsed):
        reason = f"stopped: possible paid/limit response http {status}: {redact(text)}"
        state["stop_reason"] = reason
        save_state(state)
        telegram(
            "MemeDesk: ho fermato i tentativi di creare l'A1 perché Oracle ha risposto "
            "in un modo che potrebbe non essere gratuito (limite, quota o upgrade). "
            f"Dettaglio: {redact(text)[:300]}. Nessun upgrade a Pay As You Go è stato fatto.",
            desk_env,
        )
        disable_timer()
        log(reason)
        return 3

    # Other errors (auth, bad image, subnet, ...): log and keep trying, but
    # back off a bit so a persistent 400 does not hammer the API.
    apply_backoff(state, 30 * 60)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log("unhandled error: " + redact(traceback.format_exc()))
        sys.exit(1)
