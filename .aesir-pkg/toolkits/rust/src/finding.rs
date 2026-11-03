//! One finding in the core format (v1).

/// A finding; `source` is left out so the core fills in the toolkit name.
pub struct Finding {
    pub file: String,
    pub line: usize,
    pub severity: &'static str,
    pub rule: String,
    pub message: String,
}

impl Finding {
    /// Severity as a number, higher is worse.
    pub fn rank(&self) -> u8 {
        match self.severity {
            "high" => 3,
            "medium" => 2,
            "low" => 1,
            _ => 0,
        }
    }

    /// The finding as one JSON line.
    pub fn to_json(&self) -> String {
        serde_json::json!({"v": 1, "file": self.file, "line": self.line, "severity": self.severity,
                           "rule": self.rule, "message": self.message})
        .to_string()
    }
}
