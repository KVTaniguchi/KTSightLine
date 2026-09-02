import SwiftUI

struct CartView: View {
    private let order = Order.sample

    var body: some View {
        NavigationStack {
            List {
                Section("Items") {
                    ForEach(order.items) { item in
                        HStack {
                            VStack(alignment: .leading) {
                                Text(item.name)
                                Text("Qty \(item.quantity)")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            Spacer()
                            Text(item.formattedPrice)
                                .monospacedDigit()
                                .accessibilityLabel("Price \(item.formattedPrice)")

                            // SEEDED DEFECT D-005 (hitRegion): 16x16 is far below the
                            // 44x44 minimum tap target.
                            Button {
                                // remove item
                            } label: {
                                Image(systemName: "xmark.circle.fill")
                                    .resizable()
                                    .frame(width: 16, height: 16)
                            }
                            .accessibilityLabel("Remove \(item.name)")
                            .accessibilityIdentifier("cart.removeItem")
                        }
                    }
                }

                Section {
                    NavigationLink("Continue to checkout") {
                        CheckoutSummaryView(order: order)
                    }
                    .accessibilityIdentifier("cart.continue")
                }
            }
            .navigationTitle("Cart")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    // SEEDED DEFECT D-004 (sufficientElementDescription): image-only
                    // control with no accessibilityLabel.
                    Button {
                        // open help
                    } label: {
                        Image(systemName: "questionmark.circle")
                    }
                    .accessibilityIdentifier("cart.help")
                }
                ToolbarItem(placement: .topBarLeading) {
                    Button {
                        // share cart
                    } label: {
                        Image(systemName: "square.and.arrow.up")
                            .resizable()
                            .frame(width: 14, height: 14)
                    }
                    .accessibilityLabel("Share cart")
                    .accessibilityIdentifier("cart.share")
                }
            }
        }
    }
}

#Preview {
    CartView()
}
