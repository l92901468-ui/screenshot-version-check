"""外部识图供应商的共享控制面（demo）。

这里刻意只持久化 credential version / status，不存真正 API token。
真正 secret 仍应来自 secret manager / 环境变量，并且绝不能写入日志或审计事件。

控制面用于展示两个 GRC 语义：
1. token 暴露后，所有 worker 能共享地看到 provider 已被暂停；
2. incident workflow 有可审计的 append-only 事件轨迹。
"""

import time
import uuid

import db

PROVIDER = "external-vision"


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def ensure_schema():
    with db.POOL.connection() as con:
        cur = con.cursor()
        cur.execute(
            """CREATE TABLE IF NOT EXISTS provider_control (
                provider TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                credential_version INTEGER NOT NULL DEFAULT 1,
                token_status TEXT NOT NULL DEFAULT 'active',
                incident_status TEXT NOT NULL DEFAULT 'none',
                active_incident_id TEXT,
                updated_at TEXT NOT NULL
            )"""
        )
        # 兼容已经跑过早期 demo schema 的数据库。
        cols = {r["name"] for r in cur.execute("PRAGMA table_info(provider_control)").fetchall()}
        if "active_incident_id" not in cols:
            cur.execute("ALTER TABLE provider_control ADD COLUMN active_incident_id TEXT")

        cur.execute(
            """CREATE TABLE IF NOT EXISTS provider_incident_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                stage TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL
            )"""
        )
        cur.execute(
            """INSERT OR IGNORE INTO provider_control(
                   provider, enabled, credential_version, token_status,
                   incident_status, active_incident_id, updated_at)
               VALUES(?, 1, 1, 'active', 'none', NULL, ?)""",
            (PROVIDER, _now()),
        )
        con.commit()


def get_state(provider=PROVIDER):
    ensure_schema()
    with db.POOL.connection() as con:
        row = con.execute(
            "SELECT * FROM provider_control WHERE provider=?", (provider,)
        ).fetchone()
    return dict(row) if row else None


def _event(cur, incident_id, provider, stage, detail=""):
    cur.execute(
        """INSERT INTO provider_incident_events(
               incident_id, provider, stage, detail, created_at)
           VALUES(?,?,?,?,?)""",
        (incident_id, provider, stage, detail, _now()),
    )


def assert_active_incident(incident_id, provider=PROVIDER):
    state = get_state(provider)
    if not state or state["active_incident_id"] != incident_id or state["incident_status"] != "contained":
        raise ValueError("incident is not the active contained provider incident")
    return state


def record_event(incident_id, stage, detail="", provider=PROVIDER, *, require_active=False):
    ensure_schema()
    if require_active:
        assert_active_incident(incident_id, provider)
    with db.POOL.connection() as con:
        _event(con.cursor(), incident_id, provider, stage, detail)
        con.commit()


def begin_token_exposure_incident(reason, suspected_since="unknown", provider=PROVIDER):
    """立即 containment：记录最小证据快照、暂停 provider、撤销旧 credential generation。

    注意这里的 "rotate" 是控制面 generation bump。真实系统还必须在 secret manager / provider
    侧实际 revoke/rotate secret，并通过受控渠道重新分发新 secret。
    """
    ensure_schema()
    incident_id = "INC-" + uuid.uuid4().hex[:12]
    with db.POOL.connection() as con:
        cur = con.cursor()
        before = cur.execute(
            "SELECT * FROM provider_control WHERE provider=?", (provider,)
        ).fetchone()
        if before is None:
            raise RuntimeError("provider control row missing")
        if before["active_incident_id"]:
            raise RuntimeError(
                f"provider already has active incident {before['active_incident_id']}"
            )

        # 先记录不含 secret 的最小证据快照；不为了完整调查而延迟 containment。
        _event(
            cur,
            incident_id,
            provider,
            "detected",
            f"reason={reason}; suspected_since={suspected_since}; credential_version={before['credential_version']}",
        )
        _event(
            cur,
            incident_id,
            provider,
            "evidence_snapshot",
            "captured provider/version/timestamps; secret value intentionally excluded",
        )

        cur.execute(
            """UPDATE provider_control
               SET enabled=0,
                   credential_version=credential_version+1,
                   token_status='revoked',
                   incident_status='contained',
                   active_incident_id=?,
                   updated_at=?
               WHERE provider=?""",
            (incident_id, _now(), provider),
        )
        _event(cur, incident_id, provider, "credential_revoked_rotated", "old generation invalidated")
        _event(cur, incident_id, provider, "provider_paused", "new external requests blocked")
        _event(cur, incident_id, provider, "initial_escalation", "security/GRC owner notified; scope still under investigation")
        con.commit()

    return incident_id, get_state(provider)


def close_incident(
    incident_id,
    *,
    human_approved,
    vendor_confirmed,
    credential_deployed,
    provider=PROVIDER,
):
    """人工批准 + vendor 确认 + 新 credential 已部署后才恢复 external provider。"""
    if not human_approved:
        raise ValueError("human approval required before closing incident")
    if not vendor_confirmed:
        raise ValueError("vendor confirmation required before closing incident")
    if not credential_deployed:
        raise ValueError("rotated credential must be deployed before provider resume")

    ensure_schema()
    with db.POOL.connection() as con:
        cur = con.cursor()
        row = cur.execute(
            "SELECT * FROM provider_control WHERE provider=?", (provider,)
        ).fetchone()
        if (
            row is None
            or row["incident_status"] != "contained"
            or row["active_incident_id"] != incident_id
        ):
            raise RuntimeError("incident is not the active contained provider incident")

        _event(cur, incident_id, provider, "human_approval", "targeted remediation approved")
        _event(cur, incident_id, provider, "targeted_remediation", "affected requests/data handled according to confirmed scope")
        _event(cur, incident_id, provider, "vendor_confirmation", "provider acknowledgement/confirmation received")
        _event(cur, incident_id, provider, "credential_deployed", "rotated credential distributed through controlled channel")
        cur.execute(
            """UPDATE provider_control
               SET enabled=1,
                   token_status='active',
                   incident_status='closed',
                   active_incident_id=NULL,
                   updated_at=?
               WHERE provider=?""",
            (_now(), provider),
        )
        _event(cur, incident_id, provider, "provider_resumed", "rotated credential may be used again")
        _event(cur, incident_id, provider, "closed", "audit evidence complete")
        con.commit()
    return get_state(provider)


def list_events(incident_id):
    ensure_schema()
    with db.POOL.connection() as con:
        rows = con.execute(
            """SELECT id, incident_id, provider, stage, detail, created_at
               FROM provider_incident_events
               WHERE incident_id=? ORDER BY id""",
            (incident_id,),
        ).fetchall()
    return [dict(r) for r in rows]
