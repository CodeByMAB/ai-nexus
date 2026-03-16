#!/usr/bin/env python3
"""
Core Services Test
Tests token manager, key provisioner, and subscription manager
"""
import asyncio
import sys

from services.key_provisioner import provision_api_key
from services.token_manager import get_available_tokens, add_purchased_tokens
from services.promo_code_validator import validate_promo_code, get_promo_details
from services.subscription_manager import handle_new_subscription


async def test_promo_codes():
    """Test promo code validation"""
    print("\n" + "=" * 70)
    print("Testing Promo Code Validation")
    print("=" * 70)

    # Test FAMBAM2025
    print("\n[1] Testing FAMBAM2025...")
    valid, promo, error = await validate_promo_code("FAMBAM2025", "family")
    if valid:
        print(f"  ✓ Valid promo code: {promo['code']}")
        print(f"    • Plan: {promo['plan_tier']}")
        print(f"    • Discount: ${promo['discount_amount']:.2f}")
    else:
        print(f"  ✗ Invalid: {error}")

    # Test VANGUARD
    print("\n[2] Testing VANGUARD...")
    valid, promo, error = await validate_promo_code("VANGUARD", "beta")
    if valid:
        print(f"  ✓ Valid promo code: {promo['code']}")
        print(f"    • Plan: {promo['plan_tier']}")
        print(f"    • Discount: ${promo['discount_amount']:.2f}")
    else:
        print(f"  ✗ Invalid: {error}")

    # Test invalid code
    print("\n[3] Testing invalid code...")
    valid, promo, error = await validate_promo_code("INVALID123", "regular")
    if not valid:
        print(f"  ✓ Correctly rejected: {error}")
    else:
        print(f"  ✗ Should have been rejected")

    return True


async def test_key_provisioning():
    """Test API key provisioning"""
    print("\n" + "=" * 70)
    print("Testing API Key Provisioning")
    print("=" * 70)

    print("\n[1] Provisioning test API key...")
    success, api_key, api_key_id = await provision_api_key(
        client_email="test@example.com",
        plan_tier="regular",
        payment_method="stripe",
        subscription_id="sub_test_123",
        customer_id="cus_test_123",
        monthly_price=25.00
    )

    if success and api_key_id:
        print(f"  ✓ API key provisioned: ID {api_key_id}")
        if api_key:
            print(f"    • Key: {api_key[:20]}...")
        return api_key_id
    else:
        print(f"  ✗ Provisioning failed")
        return None


async def test_token_operations(api_key_id: int):
    """Test token operations"""
    print("\n" + "=" * 70)
    print("Testing Token Operations")
    print("=" * 70)

    # Get initial balance
    print("\n[1] Checking initial token balance...")
    balances = await get_available_tokens(api_key_id)
    print(f"  • Monthly: {balances['monthly']:,}")
    print(f"  • Purchased: {balances['purchased']:,}")
    print(f"  • Total: {balances['total']:,}")

    # Add purchased tokens
    print("\n[2] Adding token pack (Medium: 500k tokens)...")
    success, new_balances = await add_purchased_tokens(
        api_key_id=api_key_id,
        tokens_to_add=500000,
        pack_type="medium",
        price_paid=20.00,
        currency="USD"
    )

    if success:
        print(f"  ✓ Tokens added successfully")
        print(f"    • Monthly: {new_balances['monthly']:,}")
        print(f"    • Purchased: {new_balances['purchased']:,}")
        print(f"    • Total: {new_balances['total']:,}")
    else:
        print(f"  ✗ Failed to add tokens")

    return True


async def test_subscription_flow():
    """Test full subscription flow"""
    print("\n" + "=" * 70)
    print("Testing Full Subscription Flow")
    print("=" * 70)

    print("\n[1] Creating new subscription with FAMBAM2025...")
    success, api_key, api_key_id, payment_event_id = await handle_new_subscription(
        customer_email="family@example.com",
        plan_tier="family",
        payment_method="stripe",
        amount=25.00,  # Base price
        currency="USD",
        subscription_id="sub_family_test",
        customer_id="cus_family_test",
        promo_code="FAMBAM2025"
    )

    if success and api_key_id:
        print(f"  ✓ Subscription created")
        print(f"    • API Key ID: {api_key_id}")
        print(f"    • Payment Event ID: {payment_event_id}")
        if api_key:
            print(f"    • API Key: {api_key[:20]}...")

        # Check token balance
        balances = await get_available_tokens(api_key_id)
        print(f"    • Initial tokens: {balances['total']:,}")
    else:
        print(f"  ✗ Subscription creation failed")

    return True


async def main():
    """Run all service tests"""
    print("\n" + "=" * 70)
    print("CORE SERVICES TEST SUITE")
    print("=" * 70)

    try:
        # Test promo codes
        await test_promo_codes()

        # Test key provisioning
        api_key_id = await test_key_provisioning()

        if api_key_id:
            # Test token operations
            await test_token_operations(api_key_id)

        # Test full subscription flow
        await test_subscription_flow()

        print("\n" + "=" * 70)
        print("✅ All service tests completed")
        print("=" * 70)
        print()

        return 0

    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
