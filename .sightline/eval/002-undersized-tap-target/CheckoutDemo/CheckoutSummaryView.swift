import SwiftUI

struct CheckoutSummaryView: View {
    let order: Order

    var body: some View {
        List {
            Section("Delivery") {
                // SEEDED DEFECT D-001 (textClipped): a fixed 40pt row height. Fits at
                // default Dynamic Type; at accessibility-extra-extra-extra-large the
                // content needs ~120pt and is cut off mid-glyph by .clipped().
                // The classic fixed-height-cell bug: invisible in the diff, invisible
                // at default size, obvious in a screenshot at AX5.
                VStack(alignment: .leading, spacing: 2) {
                    Text("Estimated delivery")
                        .accessibilityIdentifier("checkout.estimatedDeliveryLabel")
                    Text(order.deliveryEstimate)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .accessibilityIdentifier("checkout.estimatedDeliveryValue")
                }
                .fixedSize()
                .frame(maxWidth: .infinity, minHeight: 44, maxHeight: 44, alignment: .topLeading)
                .clipped()
            }

            Section("Order") {
                ForEach(order.items) { item in
                    HStack {
                        Text(item.name)
                        Spacer()
                        Text(item.formattedPrice)
                            .monospacedDigit()
                            .accessibilityLabel("Price \(item.formattedPrice)")
                    }
                }
                HStack {
                    Text("Subtotal").fontWeight(.semibold)
                    Spacer()
                    Text(order.formattedSubtotal)
                        .fontWeight(.semibold)
                        .monospacedDigit()
                        .accessibilityLabel("Subtotal \(order.formattedSubtotal)")
                        .accessibilityIdentifier("checkout.subtotal")
                }
            }

            Section {
                NavigationLink("Payment method") {
                    PaymentMethodView(order: order)
                }
                .accessibilityIdentifier("checkout.payment")
            }
        }
        .navigationTitle("Checkout")
        .navigationBarTitleDisplayMode(.inline)
    }
}

#Preview {
    NavigationStack { CheckoutSummaryView(order: .sample) }
}
