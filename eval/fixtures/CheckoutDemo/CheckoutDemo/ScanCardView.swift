import AVFoundation
import SwiftUI

struct ScanCardView: View {
    @State private var status = "Tap to start the camera."

    var body: some View {
        VStack(spacing: 20) {
            Image(systemName: "creditcard.viewfinder")
                .font(.system(size: 64))
                .foregroundStyle(.tint)
                .accessibilityHidden(true)

            Text(status)
                .multilineTextAlignment(.center)
                .accessibilityIdentifier("scanCard.status")

            Button("Start camera") {
                requestCameraAccess()
            }
            .buttonStyle(.borderedProminent)
            .accessibilityIdentifier("scanCard.start")
        }
        .padding()
        .navigationTitle("Scan card")
        .navigationBarTitleDisplayMode(.inline)
    }

    private func requestCameraAccess() {
        // SEEDED DEFECT D-003 (missing-usage-description): AVCaptureDevice camera
        // access with no NSCameraUsageDescription in Info.plist. This is a hard crash
        // on first tap, and no unit test reaches it.
        AVCaptureDevice.requestAccess(for: .video) { granted in
            Task { @MainActor in
                status = granted ? "Camera ready." : "Camera access denied."
            }
        }
    }
}

#Preview {
    NavigationStack { ScanCardView() }
}
