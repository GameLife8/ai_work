INCIDENT_TABLE_SQL = """
CREATE TABLE incident (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    incident_no VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL,
    priority VARCHAR(32),
    root_alert_event_id BIGINT,
    root_node_key VARCHAR(255),
    summary TEXT,
    first_seen_at DATETIME,
    last_seen_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_incident_no (incident_no)
);
"""
