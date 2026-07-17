import SwiftUI

@main
struct BruceApp: App {
    init() { Reporter.installCrashHandler() }   // content-free, local-only (see Reporter.swift)
    var body: some Scene {
        WindowGroup {
            RootFlow()
        }
    }
}
