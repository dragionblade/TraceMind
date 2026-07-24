import { calculateDiscountedPrice, ProductData } from "./utils/helper";

const item: ProductData = {
    id: 101,
    name: "Wireless Keyboard",
    priceUsd: 49.99,
    discountRate: 0.10
};

console.log(`Processing order for ${item.name}...`);
try {
    const finalPrice = calculateDiscountedPrice(item);
    if (isNaN(finalPrice)) {
        throw new TypeError("Final Price is NaN! Missing required property 'price'.");
    }
    console.log(`Final Price: $${finalPrice.toFixed(2)}`);
} catch (error: any) {
    console.error("CRITICAL ERROR: Failed to compute price.");
    console.error(error.stack || error.message);
    process.exit(1);
}
