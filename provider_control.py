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
                updated_at TEXT NOT NULL
            )"""
        )
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
                   provider, enabled, credential_version, token_status, incident_status, updated_at)
               VALUES(?, 1, 1, 'active', 'none', ?)""",
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


def record_event(incident_id, stage, detail="", provider=PROVIDER):
    ensure_schema()
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
                   updated_at=?
               WHERE provider=?""",
            (_now(), provider),
        )
        _event(cur, incident_id, provider, "credential_revoked_rotated", "old generation invalidated")
        _event(cur, incident_id, provider, "provider_paused", "new external requests blocked")
        _event(cur, incident_id, provider, "initial_escalation", "security/GRC owner notified; scope still under investigation")
        con.commit()

    return incident_id, get_state(provider)


def close_incident(incident_id, *, human_approved, vendor_confirmed, provider=PROVIDER):
    """只有人工批准 + 供应商确认后才恢复 external provider。"""
    if not human_approved:
        raise ValueError("human approval required before closing incident")
    if not vendor_confirmed:
        raise ValueError("vendor confirmation required before closing incident")

    ensure_schema()
    with db.POOL.connection() as con:
        cur = con.cursor()
        row = cur.execute(
            "SELECT * FROM provider_control WHERE provider=?", (provider,)
        ).fetchone()
        if row is None or row["incident_status"] == "none":
            raise RuntimeError("no active provider incident")

        _event(cur, incident_id, provider, "human_approval", "targeted remediation approved")
        _event(cur, incident_id, provider, "targeted_remediation", "affected requests/data handled according to confirmed scope")
        _event(cur, incident_id, provider, "vendor_confirmation", "provider acknowledgement/confirmation received")
        cur.execute(
            """UPDATE provider_control
               SET enabled=1,
                   token_status='active',
                   incident_status='closed',
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
