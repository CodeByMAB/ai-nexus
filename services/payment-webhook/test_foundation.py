#!/usr/bin/env python3
"""
Foundation Test Suite
Tests database schema, configuration, and basic functionality
"""
import os
import sys
import sqlite3
from datetime import datetime, timedelta

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import settings

def test_database_schema():
    """Test that all tables and columns exist"""
    print("\n" + "="*70)
    print("Testing Database Schema")
    print("="*70)

    conn = sqlite3.connect(settings.key_db_path)
    cursor = conn.cursor()

    # Test 1: Check all tables exist
    print("\n[1] Checking tables exist...")
    required_tables = ['api_keys', 'payment_events', 'token_purchases', 'promo_codes', 'api_usage']

    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    existing_tables = [row[0] for row in cursor.fetchall()]

    for table in required_tables:
        if table in existing_tables:
            print(f"  ✓ Table '{table}' exists")
        else:
            print(f"  ✗ Table '{table}' MISSING!")
            return False

    # Test 2: Check api_keys columns
    print("\n[2] Checking api_keys columns...")
    cursor.execute("PRAGMA table_info(api_keys)")
    columns = {row[1]: row[2] for row in cursor.fetchall()}

    required_columns = {
        'plan_tier': 'TEXT',
        'promo_code': 'TEXT',
        'monthly_price': 'REAL',
        'payment_method': 'TEXT',
        'subscription_id': 'TEXT',
        'subscription_status': 'TEXT',
        'monthly_tokens_remaining': 'INTEGER',
        'purchased_tokens_remaining': 'INTEGER',
        'tokens_used_this_month': 'INTEGER',
    }

    for col_name, col_type in required_columns.items():
        if col_name in columns:
            print(f"  ✓ Column '{col_name}' ({col_type})")
        else:
            print(f"  ✗ Column '{col_name}' MISSING!")
            return False

    # Test 3: Check promo codes
    print("\n[3] Checking promo codes...")
    cursor.execute("SELECT code, plan_tier, discount_amount FROM promo_codes WHERE is_active = 1")
    promos = cursor.fetchall()

    if len(promos) >= 2:
        print(f"  ✓ Found {len(promos)} active promo codes:")
        for code, tier, discount in promos:
            print(f"    • {code} → {tier} (${discount:.2f} off)")
    else:
        print(f"  ✗ Expected at least 2 promo codes, found {len(promos)}")
        return False

    # Test 4: Check indexes
    print("\n[4] Checking indexes...")
    cursor.execute("SELECT name FROM sqlite_master WHERE type='index'")
    indexes = [row[0] for row in cursor.fetchall()]

    required_indexes = ['idx_payment_events_email', 'idx_token_purchases_api_key']
    for idx in required_indexes:
        if idx in indexes:
            print(f"  ✓ Index '{idx}' exists")
        else:
            print(f"  ~ Index '{idx}' missing (optional)")

    conn.close()
    return True

def test_configuration():
    """Test configuration loading and validation"""
    print("\n" + "="*70)
    print("Testing Configuration")
    print("="*70)

    print("\n[1] Core settings...")
    print(f"  • Database: {settings.key_db_path}")
    print(f"  • Server: {settings.host}:{settings.port}")
    print(f"  • Environment: {settings.environment}")
    print(f"  • Log level: {settings.log_level}")

    print("\n[2] Pricing configuration...")
    print(f"  Subscription Plans:")
    print(f"    • Trial: ${settings.plan_trial_price:.2f}/month")
    print(f"    • Family: ${settings.plan_family_price:.2f}/month")
    print(f"    • Regular: ${settings.plan_regular_price:.2f}/month")
    print(f"    • Ultra Privacy: ${settings.plan_ultra_privacy_price:.2f}/month")
    print(f"    • Beta: ${settings.plan_beta_price:.2f}/month")
    print(f"  Monthly tokens: {settings.monthly_token_allowance:,}")

    print(f"\n  Token Packs:")
    for pack in ['trial', 'small', 'medium', 'large']:
        details = settings.get_pack_details(pack)
        print(f"    • {pack.capitalize()}: {details['tokens']:,} tokens @ ${details['price']:.2f} (${details['cost_per_1k']:.3f}/1k)")

    print("\n[3] Payment providers...")
    if settings.stripe_secret_key and settings.stripe_secret_key.startswith('sk_'):
        print(f"  ✓ Stripe configured")
    else:
        print(f"  ⚠ Stripe not configured (set STRIPE_SECRET_KEY)")

    if settings.strike_api_key:
        print(f"  ✓ Strike configured")
    else:
        print(f"  ⚠ Strike not configured (set STRIKE_API_KEY)")

    if settings.btcpay_enabled:
        print(f"  ✓ BTCPay enabled")
    else:
        print(f"  ○ BTCPay disabled")

    print("\n[4] Email settings...")
    print(f"  • Provider: {settings.email_provider}")
    print(f"  • From: {settings.email_from_name} <{settings.email_from}>")
    if settings.email_provider == 'sendgrid' and settings.sendgrid_api_key:
        print(f"  ✓ SendGrid configured")
    else:
        print(f"  ⚠ Email not fully configured")

    print("\n[5] Security & URLs...")
    print(f"  • CORS origins: {len(settings.cors_origins_list)} configured")
    print(f"  • Rate limiting: {'Enabled' if settings.rate_limit_enabled else 'Disabled'}")
    print(f"  • Signup URL: {settings.signup_website_url}")
    print(f"  • API Docs: {settings.api_docs_url}")

    return True

def test_database_operations():
    """Test basic database operations"""
    print("\n" + "="*70)
    print("Testing Database Operations")
    print("="*70)

    conn = sqlite3.connect(settings.key_db_path)
    cursor = conn.cursor()

    # Test 1: Query existing API keys
    print("\n[1] Checking existing API keys...")
    cursor.execute("SELECT count(*) FROM api_keys")
    count = cursor.fetchone()[0]
    print(f"  • Found {count} existing API key(s)")

    if count > 0:
        cursor.execute("""
            SELECT id, client_name, client_email,
                   monthly_tokens_remaining, purchased_tokens_remaining
            FROM api_keys LIMIT 3
        """)
        for row in cursor.fetchall():
            print(f"    • ID {row[0]}: {row[1]} ({row[2]}) - {row[3]:,} monthly + {row[4]:,} purchased tokens")

    # Test 2: Query payment events
    print("\n[2] Checking payment events...")
    cursor.execute("SELECT count(*) FROM payment_events")
    count = cursor.fetchone()[0]
    print(f"  • Found {count} payment event(s)")

    # Test 3: Query token purchases
    print("\n[3] Checking token purchases...")
    cursor.execute("SELECT count(*) FROM token_purchases")
    count = cursor.fetchone()[0]
    print(f"  • Found {count} token purchase(s)")

    # Test 4: Test promo code lookup
    print("\n[4] Testing promo code lookup...")
    cursor.execute("SELECT * FROM promo_codes WHERE code = 'FAMILY2024'")
    row = cursor.fetchone()
    if row:
        print(f"  ✓ FAMILY2024 promo code found")
        print(f"    • Plan tier: {row[2]}")
        print(f"    • Discount: ${row[3]:.2f}")
        print(f"    • Uses: {row[6]}/{row[5] if row[5] else '∞'}")
    else:
        print(f"  ✗ FAMILY2024 promo code not found!")
        return False

    conn.close()
    return True

def test_pricing_logic():
    """Test pricing calculations"""
    print("\n" + "="*70)
    print("Testing Pricing Logic")
    print("="*70)

    print("\n[1] Plan pricing...")
    plans = ['trial', 'family', 'regular', 'ultra_privacy', 'beta']
    for plan in plans:
        price = settings.get_plan_price(plan)
        print(f"  • {plan}: ${price:.2f}/month ({settings.monthly_token_allowance:,} tokens)")

    print("\n[2] Token pack economics...")
    for pack in ['trial', 'small', 'medium', 'large']:
        details = settings.get_pack_details(pack)
        tokens = details['tokens']
        price = details['price']
        cost_per_1k = details['cost_per_1k']

        # Calculate value vs subscription
        sub_cost_per_1k = settings.plan_regular_price / (settings.monthly_token_allowance / 1000)
        value_diff = ((sub_cost_per_1k - cost_per_1k) / sub_cost_per_1k) * 100

        if cost_per_1k < sub_cost_per_1k:
            print(f"  ✓ {pack.capitalize()}: {tokens:,} @ ${price:.2f} (${cost_per_1k:.4f}/1k - {value_diff:.1f}% better than subscription)")
        else:
            print(f"  • {pack.capitalize()}: {tokens:,} @ ${price:.2f} (${cost_per_1k:.4f}/1k)")

    return True

def main():
    """Run all tests"""
    print("\n" + "="*70)
    print("PAYMENT WEBHOOK FOUNDATION TEST SUITE")
    print("="*70)
    print(f"Database: {settings.key_db_path}")
    print(f"Environment: {settings.environment}")
    print(f"Timestamp: {datetime.now().isoformat()}")

    results = []

    # Run tests
    results.append(("Database Schema", test_database_schema()))
    results.append(("Configuration", test_configuration()))
    results.append(("Database Operations", test_database_operations()))
    results.append(("Pricing Logic", test_pricing_logic()))

    # Summary
    print("\n" + "="*70)
    print("TEST SUMMARY")
    print("="*70)

    passed = sum(1 for _, result in results if result)
    total = len(results)

    for test_name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"  {status}: {test_name}")

    print()
    print(f"Results: {passed}/{total} tests passed")

    if passed == total:
        print("\n🎉 All tests passed! Foundation is ready.")
        return 0
    else:
        print(f"\n⚠️  {total - passed} test(s) failed. Please review errors above.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
