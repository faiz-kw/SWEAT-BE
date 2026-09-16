import uuid
from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from django.core.exceptions import ValidationError

from apps.tenant_core.context import set_tenant_db_alias
from apps.tenant_core.models import (
    Organization,
    Location,
    Branch,
    UserProfile,
    TenantUser,
    Order,
    ReferralProgram,
    ReferralIdentifier,
    Referral,
    ReferralQualificationRule,
    ReferralBenefitRule,
    RewardAccount,
    RewardLedger,
    OrderRewardRedemption,
)
from apps.tenant_core.services_rewards import ReferralRewardService


class Layer2Phase10RewardsTests(TestCase):
    databases = {'default', 'tenant_test'}

    def setUp(self):
        set_tenant_db_alias('tenant_test')
        self.org = Organization.objects.create(
            name="Sweat Rewards Org",
            code=f"ORG-{uuid.uuid4().hex[:6]}",
            status="ACTIVE"
        )
        self.loc = Location.objects.create(
            organization=self.org,
            code=f"LOC-{uuid.uuid4().hex[:4]}",
            name="Main Center",
            city="Metropolis",
            status="ACTIVE"
        )
        self.branch = Branch.objects.create(
            organization=self.org,
            location=self.loc,
            name="Downtown Studio",
            code=f"BRN-{uuid.uuid4().hex[:4]}",
            timezone="UTC"
        )

        # Referrer User & Member Profile
        self.user_referrer = TenantUser.objects.create(
            organization=self.org,
            email=f"referrer-{uuid.uuid4().hex[:6]}@example.com",
            first_name="Jordan",
            last_name="Referrer",
            status="ACTIVE"
        )
        self.profile_referrer = UserProfile.objects.create(
            user=self.user_referrer,
            member_number=f"MEM-{uuid.uuid4().hex[:6]}",
            first_name_snapshot="Jordan",
            last_name_snapshot="Referrer",
            preferred_branch=self.branch,
            member_status="ACTIVE"
        )

        # Referee User & Member Profile
        self.user_referee = TenantUser.objects.create(
            organization=self.org,
            email=f"referee-{uuid.uuid4().hex[:6]}@example.com",
            first_name="Casey",
            last_name="Referee",
            status="ACTIVE"
        )
        self.profile_referee = UserProfile.objects.create(
            user=self.user_referee,
            member_number=f"MEM-{uuid.uuid4().hex[:6]}",
            first_name_snapshot="Casey",
            last_name_snapshot="Referee",
            preferred_branch=self.branch,
            member_status="ACTIVE"
        )

        # Referral Program
        self.program = ReferralProgram.objects.create(
            organization=self.org,
            code="FRIEND-2026",
            name="Bring a Friend 2026",
            status="ACTIVE"
        )

    def test_double_entry_points_earn_and_ledger(self):
        """Points earn credits the RewardAccount and appends an immutable ledger entry."""
        ledger = ReferralRewardService.earn_rewards(
            user_profile=self.profile_referrer,
            reward_type='POINTS',
            quantity=Decimal('250.00'),
            reason_code='WORKOUT_MILESTONE',
            reason='Completed 10 workouts this month'
        )

        self.assertIsNotNone(ledger.id)
        self.assertEqual(ledger.transaction_type, 'EARN')
        self.assertEqual(ledger.quantity, Decimal('250.00'))
        self.assertEqual(ledger.balance_after, Decimal('250.00'))

        account = RewardAccount.objects.get(user_profile=self.profile_referrer)
        self.assertEqual(account.points_balance, Decimal('250.00'))
        self.assertEqual(account.lifetime_earned, Decimal('250.00'))
        self.assertEqual(account.lifetime_redeemed, Decimal('0.00'))

    def test_redeem_rewards_and_overdraft_protection(self):
        """Redeeming validates balance sufficiency and records redemptions."""
        # 1. Earn points first
        ReferralRewardService.earn_rewards(
            user_profile=self.profile_referrer,
            reward_type='POINTS',
            quantity=Decimal('100.00'),
            reason_code='INITIAL_BONUS'
        )

        # 2. Overdraft attempt should raise ValidationError
        with self.assertRaises(ValidationError):
            ReferralRewardService.redeem_rewards(
                user_profile=self.profile_referrer,
                reward_type='POINTS',
                quantity=Decimal('150.00'),
                reason_code='STORE_PURCHASE'
            )

        # 3. Valid redemption
        order = Order.objects.create(
            branch=self.branch,
            user_profile=self.profile_referrer,
            order_number=f"ORD-{uuid.uuid4().hex[:6]}",
            subtotal=Decimal('50.00'),
            total_amount=Decimal('40.00'),
            status='PAID'
        )

        ledger, redemption = ReferralRewardService.redeem_rewards(
            user_profile=self.profile_referrer,
            reward_type='POINTS',
            quantity=Decimal('50.00'),
            monetary_value=Decimal('10.00'),
            order=order,
            reason_code='ORDER_REDEMPTION'
        )

        self.assertEqual(ledger.transaction_type, 'REDEEM')
        self.assertEqual(ledger.balance_after, Decimal('50.00'))
        self.assertIsNotNone(redemption)
        self.assertEqual(redemption.units_redeemed, Decimal('50.00'))
        self.assertEqual(redemption.monetary_value, Decimal('10.00'))

        account = RewardAccount.objects.get(user_profile=self.profile_referrer)
        self.assertEqual(account.points_balance, Decimal('50.00'))
        self.assertEqual(account.lifetime_redeemed, Decimal('50.00'))

    def test_referral_code_generation_and_unique_constraint(self):
        """Referral code generator creates unique codes and prevents duplicates."""
        ident = ReferralRewardService.generate_referral_identifier(
            owner_user=self.user_referrer,
            program=self.program,
            identifier_type='MEMBER_REFERRAL_CODE'
        )

        self.assertIsNotNone(ident.id)
        self.assertTrue(ident.identifier_value.startswith('REF-'))
        self.assertEqual(ident.status, 'ACTIVE')

        # Custom code test
        ident2 = ReferralRewardService.generate_referral_identifier(
            owner_user=self.user_referrer,
            program=self.program,
            custom_code='JORDANVIP'
        )
        self.assertEqual(ident2.identifier_value, 'JORDANVIP')

        # Duplicate custom code raises ValidationError
        with self.assertRaises(ValidationError):
            ReferralRewardService.generate_referral_identifier(
                owner_user=self.user_referee,
                program=self.program,
                custom_code='JORDANVIP'
            )

    def test_prohibit_self_referral(self):
        """Users cannot refer themselves via user ID or email."""
        ident = ReferralRewardService.generate_referral_identifier(
            owner_user=self.user_referrer,
            program=self.program,
            custom_code='JORDAN-SELF'
        )

        # Self-referral via user ID
        with self.assertRaises(ValidationError):
            ReferralRewardService.register_referral(
                identifier_value='JORDAN-SELF',
                referred_user=self.user_referrer
            )

        # Self-referral via email
        with self.assertRaises(ValidationError):
            ReferralRewardService.register_referral(
                identifier_value='JORDAN-SELF',
                referred_email=self.user_referrer.email
            )

    def test_referral_registration_qualification_and_rewarding(self):
        """Full referral lifecycle: Register -> Qualify on First Purchase -> Automatic Reward Granting."""
        # 1. Generate Referrer's code
        ident = ReferralRewardService.generate_referral_identifier(
            owner_user=self.user_referrer,
            program=self.program,
            custom_code='JORDAN-SHARE'
        )

        # 2. Configure Qualification Rule (FIRST_PURCHASE min $30)
        ReferralQualificationRule.objects.create(
            referral_program=self.program,
            qualification_event='FIRST_PURCHASE',
            minimum_order_amount=Decimal('30.00'),
            priority=1,
            status='ACTIVE'
        )

        # 3. Configure Benefit Rules:
        # Referrer gets 500 POINTS
        ReferralBenefitRule.objects.create(
            referral_program=self.program,
            beneficiary='REFERRER',
            benefit_type='POINTS',
            benefit_value=Decimal('500.00'),
            priority=1,
            status='ACTIVE'
        )
        # Referee gets $15 CREDIT
        ReferralBenefitRule.objects.create(
            referral_program=self.program,
            beneficiary='REFEREE',
            benefit_type='CREDIT',
            benefit_value=Decimal('15.00'),
            priority=2,
            status='ACTIVE'
        )

        # 4. Referee registers via code
        referral = ReferralRewardService.register_referral(
            identifier_value='JORDAN-SHARE',
            referred_user=self.user_referee,
            source='MOBILE_APP'
        )
        self.assertEqual(referral.status, 'REGISTERED')
        self.assertIsNotNone(referral.registered_at)

        # 5. Referee makes first purchase qualifying order ($45.00)
        order = Order.objects.create(
            branch=self.branch,
            user_profile=self.profile_referee,
            order_number=f"ORD-{uuid.uuid4().hex[:6]}",
            subtotal=Decimal('45.00'),
            total_amount=Decimal('45.00'),
            status='PAID'
        )

        # 6. Evaluate and reward referral
        rewarded_ref = ReferralRewardService.qualify_and_reward_referral(
            referral=referral,
            event_type='FIRST_PURCHASE',
            order=order
        )

        self.assertEqual(rewarded_ref.status, 'REWARDED')
        self.assertIsNotNone(rewarded_ref.qualified_at)
        self.assertIsNotNone(rewarded_ref.rewarded_at)

        # Check Referrer got 500 points
        referrer_acc = RewardAccount.objects.get(user_profile=self.profile_referrer)
        self.assertEqual(referrer_acc.points_balance, Decimal('500.00'))

        # Check Referee got $15 credit
        referee_acc = RewardAccount.objects.get(user_profile=self.profile_referee)
        self.assertEqual(referee_acc.credit_balance, Decimal('15.00'))

        # Verify ledger entries created
        referrer_ledgers = RewardLedger.objects.filter(user_profile=self.profile_referrer)
        self.assertEqual(referrer_ledgers.count(), 1)
        self.assertEqual(referrer_ledgers.first().reason_code, 'REFERRAL_REWARD_REFERRER')

        referee_ledgers = RewardLedger.objects.filter(user_profile=self.profile_referee)
        self.assertEqual(referee_ledgers.count(), 1)
        self.assertEqual(referee_ledgers.first().reason_code, 'REFERRAL_WELCOME_REFEREE')
