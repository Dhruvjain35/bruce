import Foundation

struct MissionSummary: Codable, Identifiable, Hashable {
    let mission_id: UUID
    let status: String
    let phase: String
    let short_status: String
    var id: UUID { mission_id }
}

struct MissionCreated: Codable {
    let mission_id: UUID
    let status: String
    let phase: String
}

// MARK: - Async intake contract (matches engine POST /v1/intake -> 202, GET /v1/missions/{id})

/// 202 body: the durable mission returned immediately, before any model runs.
struct IntakeAccepted: Codable {
    let mission_id: UUID
    let source_id: UUID
    let state: String            // canonical phase, e.g. "understanding"
    let display_status: String   // e.g. "Understanding your flyer…"
    let poll: [String: String]
}

/// Canonical mission state the client polls. `extracted` / `blocking_reason` / `available_actions`
/// are populated for intake missions as they progress; nil/empty otherwise.
struct MissionDetail: Codable, Identifiable {
    let mission_id: UUID
    let status: String
    let phase: String
    let short_status: String
    let error: String?
    let version: Int
    let extracted: ExtractedIntake?
    let blocking_reason: String?
    let available_actions: [String]?
    var id: UUID { mission_id }

    var isTerminalFailure: Bool { phase == "failed" }
    var isBlocked: Bool { phase == "blocked" }
    var isReady: Bool { phase == "awaiting_approval" }
    var isWorking: Bool { phase == "understanding" || phase == "extracting" }
}

/// The grounded extraction (mirrors engine ExtractedIntake — only the fields the UI reads).
struct ExtractedIntake: Codable {
    var title: String?
    var deadlines: [ExtractedDeadline]
    var required_items: [RequiredItem]
    var cost: String?
    var location: String?
    var eligibility: String?
}

struct ExtractedDeadline: Codable, Identifiable {
    let label: String
    let date: String?
    let source_span: String
    let confidence: Double
    var id: String { label + (date ?? "") + source_span }
    var isAmbiguous: Bool { date == nil }
}

struct RequiredItem: Codable, Identifiable {
    let name: String
    let kind: String?
    let provided: Bool?
    var id: String { name }
}

struct PhaseEvent: Codable, Identifiable {
    let phase: String
    let short_status: String?
    let at: String
    var id: String { phase + at }
}

enum BruceAPIError: Error { case badStatus(Int) }

/// The slice of the API the intake session needs — injectable so its state machine is unit-testable.
protocol IntakeAPI {
    func submitIntakeText(_ text: String, idempotencyKey: String) async throws -> IntakeAccepted
    func submitIntakeBytes(_ bytes: Data, mime: String, sourceKind: String, idempotencyKey: String) async throws -> IntakeAccepted
    func mission(_ id: UUID) async throws -> MissionDetail
}
extension BruceAPI: IntakeAPI {}

struct BruceAPI {
    var base = AppConfig.baseURL
    /// Resolved per request from the signed-in session (real Bruce JWT; dev token only if allowed).
    var bearer: () -> String? = { AppSession.shared.bearer }

    private func request(_ path: String, method: String = "GET", body: Data? = nil) async throws -> Data {
        var req = URLRequest(url: base.appending(path: path))
        req.httpMethod = method
        if let token = bearer() {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }  // no token -> request is unauthenticated -> server 401 -> surfaced as session-expired
        if let body {
            req.httpBody = body
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let (data, resp) = try await URLSession.shared.data(for: req)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? -1
        guard (200..<300).contains(code) else { throw BruceAPIError.badStatus(code) }
        return data
    }

    func health() async -> Bool {
        (try? await request("/health")) != nil
    }

    func listMissions() async throws -> [MissionSummary] {
        try JSONDecoder().decode([MissionSummary].self, from: try await request("/v1/missions"))
    }

    func createResearchMission(topic: String) async throws -> MissionCreated {
        let payload: [String: Any] = [
            "student": [
                "name": "Dhruv",
                "level": "high_school",
                "background": "High-school researcher in ML for polariton / cavity-QED chemistry.",
            ],
            "goal": ["outreach_type": "research_position", "topic": topic],
            "limit": 4,
        ]
        let body = try JSONSerialization.data(withJSONObject: payload)
        let data = try await request("/v1/missions", method: "POST", body: body)
        return try JSONDecoder().decode(MissionCreated.self, from: data)
    }

    // MARK: - Async intake

    /// Submit text and get a durable mission back immediately (202). Extraction runs server-side.
    /// `idempotencyKey` makes a resubmit safe — the same key never spawns a second mission.
    func submitIntakeText(_ text: String, idempotencyKey: String) async throws -> IntakeAccepted {
        let payload: [String: Any] = ["text": text, "source_kind": "text", "idempotency_key": idempotencyKey]
        let data = try await request("/v1/intake", method: "POST", body: try JSONSerialization.data(withJSONObject: payload))
        return try JSONDecoder().decode(IntakeAccepted.self, from: data)
    }

    /// Submit a flyer/screenshot (image) or PDF as base64. `mime` e.g. "image/png", "application/pdf".
    func submitIntakeBytes(_ bytes: Data, mime: String, sourceKind: String, idempotencyKey: String) async throws -> IntakeAccepted {
        let payload: [String: Any] = [
            "content_base64": bytes.base64EncodedString(), "mime": mime,
            "source_kind": sourceKind, "idempotency_key": idempotencyKey,
        ]
        let data = try await request("/v1/intake", method: "POST", body: try JSONSerialization.data(withJSONObject: payload))
        return try JSONDecoder().decode(IntakeAccepted.self, from: data)
    }

    /// Poll canonical mission state. Safe to call immediately and repeatedly.
    func mission(_ id: UUID) async throws -> MissionDetail {
        try JSONDecoder().decode(MissionDetail.self, from: try await request("/v1/missions/\(id.uuidString.lowercased())"))
    }

    func missionEvents(_ id: UUID) async throws -> [PhaseEvent] {
        try JSONDecoder().decode([PhaseEvent].self, from: try await request("/v1/missions/\(id.uuidString.lowercased())/events"))
    }
}
