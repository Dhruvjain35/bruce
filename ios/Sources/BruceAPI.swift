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

enum BruceAPIError: Error { case badStatus(Int) }

struct BruceAPI {
    var base = DevAuth.baseURL
    var token = DevAuth.token

    private func request(_ path: String, method: String = "GET", body: Data? = nil) async throws -> Data {
        var req = URLRequest(url: base.appending(path: path))
        req.httpMethod = method
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
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
}
