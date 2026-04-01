ALERT_DECISION_TABLE_SQL = """
CREATE TABLE alert_decision (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    alert_event_id BIGINT NOT NULL,
    incident_no VARCHAR(128),
    decision_type VARCHAR(32) NOT NULL,
    priority VARCHAR(32),
    need_push TINYINT NOT NULL DEFAULT 0,
    reason_summary TEXT,
    merge_target_incident_no VARCHAR(128),
    observe_until DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""
