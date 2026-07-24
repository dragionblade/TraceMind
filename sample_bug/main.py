from utils.calculator import calculate_discounted_price


if __name__ == "__main__":
    product = {"price_usd": 49.99}
    print(f"Final Price: ${calculate_discounted_price(product):.2f}")
