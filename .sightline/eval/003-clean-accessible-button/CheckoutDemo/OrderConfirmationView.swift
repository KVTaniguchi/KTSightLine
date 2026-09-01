import SwiftUI

/// The control screen. No seeded defects — deliberately.
///
/// Semantic colors so dark mode works, no line limits so Dynamic Type reflows,
/// labels on every control, and tap targets at or above 44x44. A run that reports
/// a finding here is a false positive, and the eval corpus scores it as one.
struct OrderConfirmationView: View {
    let order: Order

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                Label("Order placed", systemImage: "checkmark.circle.fill")
                    .font(.title2.weight(.semibold))
                    .foregroundStyle(.primary)

                Text("We sent a confirmation to your email. You can track the shipment from your order history at any time.")
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)

                VStack(alignment: .leading, spacing: 8) {
                    Text("Arriving \(order.deliveryEstimate)")
                        .fixedSize(horizontal: false, vertical: true)
                    Text("Total \(order.formattedSubtotal)")
                        .monospacedDigit()
                }
                .padding()
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color(uiColor: .secondarySystemBackground))
                .clipShape(RoundedRectangle(cornerRadius: 12))

                Button {
                    // view order history
                } label: {
                    Text("View order history")
                        .frame(maxWidth: .infinity, minHeight: 44)
                }
                .buttonStyle(.borderedProminent)
                .accessibilityIdentifier("confirmation.viewHistory")

                Button {
                    // contact support
                } label: {
                    Label("Contact support", systemImage: "bubble.left.and.bubble.right")
                        .frame(maxWidth: .infinity, minHeight: 44)
                }
                .buttonStyle(.bordered)
                .accessibilityIdentifier("confirmation.contactSupport")
            }
            .padding()
        }
        .navigationTitle("Confirmation")
        .navigationBarTitleDisplayMode(.inline)
    }
}

#Preview {
    NavigationStack { OrderConfirmationView(order: .sample) }
}
