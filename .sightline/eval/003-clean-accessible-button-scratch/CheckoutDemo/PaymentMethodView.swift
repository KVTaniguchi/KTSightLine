import SwiftUI

struct PaymentMethodView: View {
    let order: Order
    @State private var selected = "Visa •••• 4242"

    private let methods = ["Visa •••• 4242", "Amex •••• 1005", "Apple Pay"]

    var body: some View {
        List {
            Section("Pay with") {
                ForEach(methods, id: \.self) { method in
                    HStack {
                        Text(method)
                        Spacer()
                        if method == selected {
                            Image(systemName: "checkmark")
                                .accessibilityLabel("Selected")
                        }
                    }
                    .contentShape(Rectangle())
                    .onTapGesture { selected = method }
                }
            }

            Section {
                // SEEDED DEFECT D-002 (contrast): hardcoded light-grey on hardcoded
                // white. Legible-ish in light mode, invisible in dark mode, and below
                // the contrast minimum in both.
                Text("Your card is charged when the order ships.")
                    .font(.footnote)
                    .foregroundColor(Color(red: 0.78, green: 0.78, blue: 0.80))
                    .padding(12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color(red: 1.0, green: 1.0, blue: 1.0))
                    .accessibilityIdentifier("payment.disclosure")
            }

            Section {
                NavigationLink("Scan a new card") {
                    ScanCardView()
                }
                .accessibilityIdentifier("payment.scanCard")

                NavigationLink("Place order") {
                    OrderConfirmationView(order: order)
                }
                .accessibilityIdentifier("payment.placeOrder")
            }
        }
        .navigationTitle("Payment")
        .navigationBarTitleDisplayMode(.inline)
    }
}

#Preview {
    NavigationStack { PaymentMethodView(order: .sample) }
}
