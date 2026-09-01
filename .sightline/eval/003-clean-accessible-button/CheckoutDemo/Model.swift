import Foundation

struct LineItem: Identifiable, Hashable {
    let id = UUID()
    let name: String
    let priceCents: Int
    let quantity: Int

    var formattedPrice: String {
        let dollars = Double(priceCents * quantity) / 100
        return dollars.formatted(.currency(code: "USD"))
    }
}

struct Order {
    var items: [LineItem]
    var deliveryEstimate: String

    var subtotalCents: Int {
        items.reduce(0) { $0 + $1.priceCents * $1.quantity }
    }

    var formattedSubtotal: String {
        (Double(subtotalCents) / 100).formatted(.currency(code: "USD"))
    }

    static let sample = Order(
        items: [
            LineItem(name: "Merino Crew Sweater", priceCents: 12800, quantity: 1),
            LineItem(name: "Selvedge Denim, 32x32", priceCents: 18500, quantity: 1),
            LineItem(name: "Leather Belt", priceCents: 6400, quantity: 2),
        ],
        deliveryEstimate: "Thursday, September 10"
    )
}
