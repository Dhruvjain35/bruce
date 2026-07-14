import Foundation

// DEV ONLY. A long-lived token signed with the local dev HS256 secret so the simulator app can
// talk to the local engine. Real auth becomes Sign in with Apple -> Supabase; this is replaced then.
enum DevAuth {
    static let baseURL = URL(string: "http://127.0.0.1:8000")!
    static let token =
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMTExMTExMS0xMTExLTExMTEtMTExMS0xMTExMTExMTExMTEiLCJleHAiOjE4MTU1MjMzOTcsImF1ZCI6ImF1dGhlbnRpY2F0ZWQifQ.h1HeZ2MT9s0ZlaKOzY-RC-icdR4gJF-sJMR_P6ug--k"
}
