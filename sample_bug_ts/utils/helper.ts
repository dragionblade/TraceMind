export interface ProductData {
    id: number;
    name: string;
    priceUsd: number;
    discountRate: number;
}

export function calculateDiscountedPrice(product: ProductData): number {
    // BUG: Looking for 'price' which doesn't exist on interface (should be 'priceUsd')
    const basePrice = (product as any).price;
    const discount = product.discountRate || 0;
    return basePrice * (1 - discount);
}
