ALERT_EVENT_TABLE_SQL = """
CREATE TABLE alert_event (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    source VARCHAR(32) NOT NULL DEFAULT 'zabbix',
    source_event_id VARCHAR(128),
    source_problem_id VARCHAR(128),
    trigger_id VARCHAR(128),
    host_id VARCHAR(128),
    host_name VARCHAR(255),
    host_ip VARCHAR(64),
    severity VARCHAR(32),
    status VARCHAR(32),
    alert_name VARCHAR(512),
    alert_message TEXT,
    tags_json JSON,
    event_time DATETIME,
    ingest_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    raw_payload_json JSON,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""
