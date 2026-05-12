-- Reports metadata table for generated markdown/PDF artifacts.

CREATE TABLE IF NOT EXISTS reports (
    id            VARCHAR(20) PRIMARY KEY,
    report_type   VARCHAR(20) NOT NULL,
    site_id       VARCHAR(40),
    assessment_id VARCHAR(50),
    team_name     VARCHAR(100),
    language      VARCHAR(10) DEFAULT 'en',
    file_path     VARCHAR(500),
    status        VARCHAR(20) NOT NULL DEFAULT 'generating',
    created_by    VARCHAR(100),
    error_message TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status);
CREATE INDEX IF NOT EXISTS idx_reports_created_at ON reports(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reports_type ON reports(report_type);
CREATE INDEX IF NOT EXISTS idx_reports_site_id ON reports(site_id);
CREATE INDEX IF NOT EXISTS idx_reports_assessment_id ON reports(assessment_id);
