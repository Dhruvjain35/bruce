import SwiftUI

// Phase 1.6: everything is mock. Realistic student data so the flow feels real before backend wiring.

// MARK: - Semantic status (color ONLY for meaning; words carry the state)

enum Status {
    case working      // neutral — Bruce is doing something / waiting on the world
    case needsYou     // amber   — a decision is required soon
    case verified     // green   — externally verified completion
    case failed       // red     — an actual failure

    var accent: Color {
        switch self {
        case .working:  return Theme.textSecondary
        case .needsYou: return Theme.amber
        case .verified: return Theme.green
        case .failed:   return Theme.red
        }
    }
    /// Symbols are quiet: none for neutral or a plain decision, a real mark only for verified/failed.
    var symbol: String? {
        switch self {
        case .working:  return nil
        case .needsYou: return nil
        case .verified: return "checkmark.seal.fill"
        case .failed:   return "exclamationmark.triangle.fill"
        }
    }
}

// MARK: - Evidence + draft

struct EvidenceSource: Identifiable {
    let id = UUID()
    let icon: String
    let kind: String
    let title: String
    let meta: String
}

struct DraftEmail {
    let to: String
    let toRole: String
    let subject: String
    let body: String
    let grounded: [String]
}

// MARK: - Canonical objects referenced across the app

struct Person: Identifiable {
    let id = UUID()
    let name: String
    let role: String
    let institution: String
    let relevance: String        // "Strong match"
    let topics: [String]
    let facultyURL: String
    let verified: Bool
    let paper: EvidenceSource
    let alternates: [String]
}

struct DocItem: Identifiable {
    let id = UUID()
    let name: String
    let done: Bool
    let note: String             // "Requested" / "Drafted" / "Needs you"
}

struct TimelineEvent: Identifiable {
    let id = UUID()
    let time: String
    let text: String
}

struct FollowUp {
    let waitDays: Int
    let maxFollowUps: Int
    let stopCondition: String
    let enabled: Bool
}

struct Receipt {
    let to: String
    let deliveredAt: String
    let note: String
}

// MARK: - Mission (phase-based, no arbitrary percentages)

struct Mission: Identifiable {
    let id = UUID()
    let title: String
    var status: Status
    var statusText: String     // mutated live as the mission progresses
    var homeLine: String
    let listLine: String
    let stateSentence: String
    let completed: [String]
    var now: String
    let next: String           // "" when there's no concrete next step yet
    let evidence: [EvidenceSource]
    let draft: DraftEmail?
    let count: MissionCount?   // real numeric progress ONLY (e.g. 3 of 5 documents)
    let updated: String
    // Workspace modules (defaulted so existing call sites are unaffected):
    var person: Person? = nil
    var documents: [DocItem] = []
    var afterApproval: [String] = []
    var timeline: [TimelineEvent] = []
    var followUp: FollowUp? = nil
    var receipt: Receipt? = nil
}

struct MissionCount { let done: Int; let total: Int; let noun: String }

struct Decision: Identifiable {
    let id = UUID()
    let title: String
    let source: String        // "Prof. Huo · Research outreach"
    let context: String
    let cta: String
    let status: Status
    let detail: String?       // consequence summary for higher-stakes actions
}

enum CalState {
    case added, needsReview, conflict, notAdded
    var text: String {
        switch self {
        case .added: return "Added"
        case .needsReview: return "Needs review"
        case .conflict: return "Conflict"
        case .notAdded: return "Not added"
        }
    }
    var color: Color {
        switch self {
        case .added: return Theme.green
        case .needsReview, .conflict: return Theme.amber
        case .notAdded: return Theme.textSecondary
        }
    }
    var symbol: String {
        switch self {
        case .added: return "checkmark.circle.fill"
        case .needsReview: return "circle.dashed"
        case .conflict: return "exclamationmark.triangle.fill"
        case .notAdded: return "plus.circle"
        }
    }
}

struct CalProposal: Identifiable {
    let id = UUID()
    let title: String
    let day: String
    let time: String
    let source: String
    let state: CalState
    var mon: String { day.split(separator: " ").first.map(String.init) ?? "" }
    var num: String { day.split(separator: " ").last.map(String.init) ?? "" }
}

struct ComingUp: Identifiable {
    let id = UUID()
    let title: String
    let when: String
}

// MARK: - Automation policy

enum AutomationMode: String, CaseIterable, Identifiable {
    case reviewAll = "Review everything"
    case smartAuto = "Smart Auto"
    case custom = "Custom"
    var id: String { rawValue }
    var blurb: String {
        switch self {
        case .reviewAll: return "Bruce prepares work and waits for your approval."
        case .smartAuto: return "Bruce handles safe, reversible actions. It asks before messages, uploads, submissions, sharing data, or uncertain decisions."
        case .custom: return "Choose rules for each action type."
        }
    }
}

struct AutoAction: Identifiable { let id = UUID(); let title: String; let when: String }

/// Dev-only hooks so each screen/state can be screenshotted deterministically via SIMCTL_CHILD_*.
enum Demo {
    static let env = ProcessInfo.processInfo.environment
    static let present = env["BRUCE_PRESENT"]   // detail | approval | handoff | clarify | failure | delete
    static let state = env["BRUCE_STATE"]       // empty | loading | undo | offline
    static let onboard = env["BRUCE_ONBOARD"] == "1"
}

enum Mock {
    static let studentName = "Dhruv"
    static let greeting = "Good afternoon"

    static let missions: [Mission] = [
        Mission(
            title: "Research outreach",
            status: .needsYou,
            statusText: "Approval needed",
            homeLine: "Review email to Prof. Huo",
            listLine: "1 email ready for Prof. Huo",
            stateSentence: "Email prepared for Prof. Huo. Bruce needs your approval before sending.",
            completed: ["Understood your polariton project",
                        "Found and verified Prof. Huo",
                        "Drafted a grounded introduction"],
            now: "Review the email",
            next: "Send it and confirm delivery",
            evidence: [
                EvidenceSource(icon: "doc.text.fill", kind: "Paper", title: "Cavity-modified reactivity in polaritonic chemistry", meta: "OpenAlex · Huo et al. · 2025"),
                EvidenceSource(icon: "person.crop.rectangle.fill", kind: "Faculty", title: "Pengfei Huo — Associate Professor", meta: "chem.rochester.edu · email verified"),
            ],
            draft: DraftEmail(
                to: "Prof. Pengfei Huo",
                toRole: "Dept. of Chemistry, University of Rochester",
                subject: "HS student working on polariton chemistry — quick question",
                body: "Dear Professor Huo,\n\nI'm a high-school student building a machine-learning model for polariton chemistry, and I read your 2025 paper on cavity-modified reactivity. Your result on vibrational strong coupling shifting reaction rates is close to what I'm trying to reproduce.\n\nI'd value 15 minutes to ask how you'd validate a learned closure against your data. I've attached a one-page summary of my approach.\n\nThank you for your time.\nDhruv",
                grounded: [
                    "His 2025 paper is real (OpenAlex verified)",
                    "Email confirmed on the Rochester faculty page",
                    "No claims about him that aren't in the sources",
                ]
            ),
            count: nil,
            updated: "12s ago",
            person: Person(
                name: "Pengfei Huo", role: "Associate Professor of Chemistry",
                institution: "University of Rochester", relevance: "Strong match",
                topics: ["Polariton chemistry", "Vibrational strong coupling", "Cavity QED"],
                facultyURL: "chem.rochester.edu/people/faculty/huo", verified: true,
                paper: EvidenceSource(icon: "doc.text.fill", kind: "Paper",
                                      title: "Cavity-modified reactivity in polaritonic chemistry",
                                      meta: "OpenAlex · Huo et al. · 2025"),
                alternates: ["A. Mandal — Penn State", "J. Yuen-Zhou — UC San Diego"]
            ),
            afterApproval: ["Send the email", "Confirm delivery", "Watch for a reply for 5 days"],
            timeline: [
                TimelineEvent(time: "9:03 PM", text: "Mission started"),
                TimelineEvent(time: "9:04 PM", text: "Faculty match found"),
                TimelineEvent(time: "9:04 PM", text: "Paper verified on OpenAlex"),
                TimelineEvent(time: "9:05 PM", text: "Draft prepared"),
            ],
            followUp: FollowUp(waitDays: 5, maxFollowUps: 1, stopCondition: "Stop after any reply", enabled: true)
        ),
        Mission(
            title: "Science Fair registration",
            status: .working,
            statusText: "Checking the submitted form",
            homeLine: "Checking the submitted form",
            listLine: "Fee and permission slip tracked",
            stateSentence: "Registration is filled. Bruce is confirming the fee and permission slip before marking it done.",
            completed: ["Read the flyer you forwarded", "Pre-filled the registration"],
            now: "Confirming the fee and permission slip",
            next: "Save the receipt and update your calendar",
            evidence: [
                EvidenceSource(icon: "doc.richtext.fill", kind: "Source", title: "Regional Science Fair flyer.pdf", meta: "You forwarded · Jul 11"),
            ],
            draft: nil,
            count: nil,
            updated: "3m ago"
        ),
        Mission(
            title: "Summer program application",
            status: .working,
            statusText: "Collecting your documents",
            homeLine: "3 of 5 documents collected",
            listLine: "3 of 5 documents collected",
            stateSentence: "Bruce broke the application into 5 items and is gathering what it can. Two still need you.",
            completed: ["Read the program requirements", "Drafted both essays", "Requested your transcript"],
            now: "Collecting your documents",
            next: "Check each against the rubric, then submit",
            evidence: [
                EvidenceSource(icon: "globe", kind: "Program", title: "Application requirements", meta: "program.example.edu · Jul 9"),
            ],
            draft: nil,
            count: MissionCount(done: 3, total: 5, noun: "documents"),
            updated: "1h ago",
            documents: [
                DocItem(name: "Personal essay", done: true, note: "Drafted"),
                DocItem(name: "Activity résumé", done: true, note: "Drafted"),
                DocItem(name: "Transcript", done: true, note: "Requested"),
                DocItem(name: "Recommendation letter", done: false, note: "Needs you — pick a teacher"),
                DocItem(name: "Parent signature", done: false, note: "Needs you"),
            ],
            afterApproval: ["Compare each item against the rubric", "Assemble the package", "Ask before submitting"],
            timeline: [
                TimelineEvent(time: "Jul 9", text: "Requirements read"),
                TimelineEvent(time: "Jul 10", text: "Essays drafted"),
                TimelineEvent(time: "Jul 11", text: "Transcript requested"),
            ]
        ),
        Mission(
            title: "Volunteer hours log",
            status: .failed,
            statusText: "The school portal rejected the upload",
            homeLine: "Upload to the school portal failed",
            listLine: "Upload didn't go through",
            stateSentence: "Bruce filled your hours log, but the school portal rejected the upload. It won't keep retrying without you.",
            completed: ["Totaled your logged hours", "Filled the portal form"],
            now: "Retry the upload",
            next: "Confirm the portal accepted it",
            evidence: [
                EvidenceSource(icon: "envelope.fill", kind: "Source", title: "Volunteer coordinator email", meta: "You forwarded · Jul 12"),
            ],
            draft: nil,
            count: nil,
            updated: "20m ago"
        ),
    ]

    static var failureMission: Mission { missions.first { $0.status == .failed } ?? missions[0] }
    static var needsYou: [Mission] { missions.filter { $0.status == .needsYou || $0.status == .failed } }
    static var working: [Mission] { missions.filter { $0.status == .working } }

    // Home "TODAY" line counts
    static let todayDeadlines = 2
    static var todayDecisions: Int { decisions.count }
    static var activeMissions: Int { missions.count }

    static let comingUp: [ComingUp] = [
        ComingUp(title: "Summer program application", when: "closes in 6 days"),
        ComingUp(title: "Science Fair registration", when: "due Feb 28"),
    ]

    static let decisions: [Decision] = [
        Decision(title: "Send outreach email",
                 source: "Prof. Huo · Research outreach",
                 context: "Grounded in his 2025 paper. You can edit before it sends.",
                 cta: "Review email", status: .needsYou,
                 detail: "To Prof. Huo · No attachment · Editable before it sends"),
        Decision(title: "Add 2 deadlines to your calendar",
                 source: "Science Fair · Summer program",
                 context: "Bruce found two dates. It won't touch your calendar without your OK.",
                 cta: "Review dates", status: .needsYou,
                 detail: "Adds to your calendar · Nothing deleted · Reversible"),
        Decision(title: "Pick a recommender",
                 source: "Summer program",
                 context: "The application needs one teacher rec. Choose who to ask.",
                 cta: "Choose", status: .needsYou, detail: nil),
    ]

    static let calendar: [CalProposal] = [
        CalProposal(title: "Science Fair registration due", day: "Feb 28", time: "11:59 PM",
                    source: "From the flyer you forwarded", state: .added),
        CalProposal(title: "Summer program deadline", day: "Mar 15", time: "5:00 PM",
                    source: "From the requirements page", state: .added),
        CalProposal(title: "Club meeting", day: "Mar 8", time: "3:30 PM",
                    source: "From a forwarded email", state: .needsReview),
        CalProposal(title: "Call with Prof. Huo (tentative)", day: "Mar 3", time: "4:00 PM",
                    source: "If he replies to the intro email", state: .conflict),
        CalProposal(title: "Scholarship info session", day: "Mar 20", time: "6:00 PM",
                    source: "Found in an opportunity listing", state: .notAdded),
    ]

    // Onboarding
    static let focusAreas = [
        "Deadlines and assignments",
        "Opportunities and scholarships",
        "Calendar and events",
        "Applications",
        "Important school email",
    ]

    // Automation
    static let canAuto = [
        "Add conflict-free deadlines",
        "Create internal tasks",
        "Organize forwarded content",
        "Update mission status",
        "Send scheduled Bruce notifications",
    ]
    static let alwaysAsk = [
        "Send an email",
        "Upload files externally",
        "Share personal data",
        "Submit a form or application",
        "Contact a new person",
        "Anything irreversible",
    ]
    static let recentAuto = [
        AutoAction(title: "Added Science Fair deadline", when: "2h ago"),
        AutoAction(title: "Organized summer-program documents", when: "1h ago"),
        AutoAction(title: "Moved a study block after a conflict", when: "12m ago"),
    ]

    struct Integration: Identifiable { let id = UUID(); let name: String; let icon: String; let status: String }

    // Full integrations catalog for the Integrations page (grouped).
    struct IntegrationItem: Identifiable { let id = UUID(); let name: String; let icon: String; let status: String; let detail: String }
    struct IntegrationSection: Identifiable { let id = UUID(); let title: String; let items: [IntegrationItem] }
    static let integrationCatalog: [IntegrationSection] = [
        IntegrationSection(title: "Calendar", items: [
            IntegrationItem(name: "Apple Calendar", icon: "calendar", status: "Connected", detail: "Deadlines and events"),
            IntegrationItem(name: "Google Calendar", icon: "calendar", status: "Available", detail: "Deadlines and events"),
        ]),
        IntegrationSection(title: "Email", items: [
            IntegrationItem(name: "Gmail", icon: "envelope.fill", status: "Connected", detail: "Reads important school mail"),
            IntegrationItem(name: "Outlook", icon: "envelope.fill", status: "Available", detail: "Reads important school mail"),
            IntegrationItem(name: "Forward-to-Bruce address", icon: "arrowshape.turn.up.right.fill", status: "Available", detail: "Forward anything to your Bruce inbox"),
        ]),
        IntegrationSection(title: "School systems", items: [
            IntegrationItem(name: "Google Classroom", icon: "graduationcap.fill", status: "Available", detail: "Assignments and due dates"),
            IntegrationItem(name: "Canvas", icon: "book.closed.fill", status: "Requires school approval", detail: "Assignments and due dates"),
            IntegrationItem(name: "Microsoft Teams", icon: "person.2.fill", status: "Coming later", detail: "Class messages"),
        ]),
        IntegrationSection(title: "Files", items: [
            IntegrationItem(name: "Google Drive", icon: "folder.fill", status: "Available", detail: "Attach documents to missions"),
            IntegrationItem(name: "iCloud Drive", icon: "icloud.fill", status: "Available", detail: "Attach documents to missions"),
        ]),
    ]
    static func integrationColor(_ s: String) -> Color {
        switch s {
        case "Connected": return Theme.green
        case "Requires school approval": return Theme.amber
        case "Coming later": return Theme.textTertiary
        default: return Theme.textSecondary
        }
    }
    static let integrations: [Integration] = [
        Integration(name: "Google Classroom", icon: "graduationcap.fill", status: "Available"),
        Integration(name: "Canvas", icon: "book.closed.fill", status: "Requires school approval"),
        Integration(name: "School email", icon: "envelope.fill", status: "Available"),
        Integration(name: "Microsoft Teams", icon: "person.2.fill", status: "Coming later"),
        Integration(name: "Forward-to-Bruce address", icon: "arrowshape.turn.up.right.fill", status: "Available"),
    ]
}
