"""
Layer 2 Business Services: Module C (Terms) & Module D (Programs / Packages / Catalog)

Key Rules Enforced:
1. Package Immutability: Published package versions (ACTIVE/RETIRED) are strictly immutable.
   Any commercial changes (duration, name, entitlements, pricing) require cloning into a new PackageVersion.
2. Terms Versioning & Acceptance: Only ACTIVE terms document versions may be accepted.
   Published terms versions are immutable. Acceptances are append-only.
3. Branch-specific pricing & availability resolution.
4. Transactional audit and domain outbox event emission.
"""

import uuid
from decimal import Decimal
from typing import Optional, Dict, Any, List
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError

from .models_catalog import (
    TermsDocument, TermsDocumentVersion, TermsAcceptance,
    ProgramCategory, Program, Package, PackageVersion,
    PackagePrice, PackageBranchAvailability, PackageEntitlementDefinition,
)
from .models_org import Organization, Branch
from .models_users import TenantUser
from .models_crm import Lead
from .models_workforce import UserProfile
from .services_reliability import record_business_audit, enqueue_outbox_event


class PackageCatalogService:
    """
    Service managing Programs, Packages, immutable PackageVersions,
    Pricing, Branch Availability, and Entitlement Definitions.
    """

    @classmethod
    @transaction.atomic
    def create_program(
        cls,
        organization: Organization,
        code: str,
        name: str,
        program_type: str = 'MEMBERSHIP',
        category_id: Optional[str] = None,
        description: Optional[str] = None,
        trial_allowed: bool = False,
        status: str = 'ACTIVE',
        actor: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> Program:
        alias = db_alias or 'default'
        program = Program(
            organization=organization,
            code=code.strip().upper(),
            name=name.strip(),
            program_type=program_type,
            category_id=category_id,
            description=description,
            trial_allowed=trial_allowed,
            status=status,
        )
        program.save(using=alias)

        record_business_audit(
            organization=organization,
            module='core',
            action_code='PROGRAM_CREATED',
            entity_type='Program',
            entity_id=program.id,
            actor_user=actor,
            event_description=f"Created program {program.name} ({program.code})",
            after_data={'code': program.code, 'name': program.name, 'program_type': program.program_type},
            db_alias=alias,
        )
        return program

    @classmethod
    @transaction.atomic
    def create_package(
        cls,
        organization: Organization,
        code: str,
        name: str,
        program_id: Optional[str] = None,
        status: str = 'ACTIVE',
        actor: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> Package:
        alias = db_alias or 'default'
        package = Package(
            organization=organization,
            program_id=program_id,
            code=code.strip().upper(),
            name=name.strip(),
            status=status,
        )
        package.save(using=alias)

        record_business_audit(
            organization=organization,
            module='core',
            action_code='PACKAGE_CREATED',
            entity_type='Package',
            entity_id=package.id,
            actor_user=actor,
            event_description=f"Created package {package.name} ({package.code})",
            after_data={'code': package.code, 'name': package.name},
            db_alias=alias,
        )
        return package

    @classmethod
    @transaction.atomic
    def create_package_version(
        cls,
        package: Package,
        name_snapshot: str,
        duration_value: int,
        duration_unit: str,
        created_by_user: TenantUser,
        description_snapshot: Optional[str] = None,
        validity_days: Optional[int] = None,
        is_trial_package: bool = False,
        effective_from: Optional[timezone.datetime] = None,
        effective_until: Optional[timezone.datetime] = None,
        status: str = 'DRAFT',
        db_alias: Optional[str] = None,
    ) -> PackageVersion:
        alias = db_alias or 'default'
        latest_version = PackageVersion.objects.using(alias).filter(
            package=package
        ).order_by('-version_number').first()
        next_ver = (latest_version.version_number + 1) if latest_version else 1

        effective_from = effective_from or timezone.now()

        pkg_version = PackageVersion(
            package=package,
            version_number=next_ver,
            name_snapshot=name_snapshot.strip(),
            description_snapshot=description_snapshot,
            duration_value=duration_value,
            duration_unit=duration_unit,
            validity_days=validity_days,
            is_trial_package=is_trial_package,
            effective_from=effective_from,
            effective_until=effective_until,
            status=status,
            created_by_user=created_by_user,
        )
        pkg_version.save(using=alias)

        record_business_audit(
            organization=package.organization,
            module='core',
            action_code='PACKAGE_VERSION_CREATED',
            entity_type='PackageVersion',
            entity_id=pkg_version.id,
            actor_user=created_by_user,
            event_description=f"Created version {next_ver} for package {package.code}",
            after_data={'package_id': str(package.id), 'version_number': next_ver, 'status': status},
            db_alias=alias,
        )
        return pkg_version

    @classmethod
    @transaction.atomic
    def publish_package_version(
        cls,
        package_version_id: str,
        actor: TenantUser,
        effective_from: Optional[timezone.datetime] = None,
        db_alias: Optional[str] = None,
    ) -> PackageVersion:
        alias = db_alias or 'default'
        pkg_ver = PackageVersion.objects.using(alias).select_for_update().get(id=package_version_id)
        now = timezone.now()

        # Validate that prices and entitlements are defined before publishing
        price_count = PackagePrice.objects.using(alias).filter(package_version=pkg_ver, status='ACTIVE').count()
        if price_count == 0:
            raise ValidationError("Cannot publish package version without at least one active package price.")

        entitlement_count = PackageEntitlementDefinition.objects.using(alias).filter(
            package_version=pkg_ver, status='ACTIVE'
        ).count()
        if entitlement_count == 0:
            raise ValidationError("Cannot publish package version without at least one entitlement definition.")

        # Retire prior active versions
        prior_active = PackageVersion.objects.using(alias).filter(
            package=pkg_ver.package,
            status='ACTIVE'
        ).exclude(id=pkg_ver.id)

        for prior in prior_active:
            prior.status = 'RETIRED'
            prior.effective_until = now
            prior.save(using=alias, update_fields=['status', 'effective_until', 'updated_at'])

        pkg_ver.status = 'ACTIVE'
        pkg_ver.effective_from = effective_from or now
        pkg_ver.save(using=alias, update_fields=['status', 'effective_from', 'updated_at'])

        record_business_audit(
            organization=pkg_ver.package.organization,
            module='core',
            action_code='PACKAGE_VERSION_PUBLISHED',
            entity_type='PackageVersion',
            entity_id=pkg_ver.id,
            actor_user=actor,
            event_description=f"Published package version {pkg_ver}",
            after_data={'status': 'ACTIVE', 'effective_from': pkg_ver.effective_from.isoformat()},
            db_alias=alias,
        )
        enqueue_outbox_event(
            organization=pkg_ver.package.organization,
            event_type='PACKAGE_PUBLISHED',
            aggregate_type='PackageVersion',
            aggregate_id=pkg_ver.id,
            payload={'package_id': str(pkg_ver.package_id), 'version_number': pkg_ver.version_number},
            db_alias=alias,
        )
        return pkg_ver

    @classmethod
    @transaction.atomic
    def modify_package_version_safely(
        cls,
        package_version_id: str,
        actor: TenantUser,
        modifications: Dict[str, Any],
        db_alias: Optional[str] = None,
    ) -> PackageVersion:
        alias = db_alias or 'default'
        existing = PackageVersion.objects.using(alias).select_for_update().get(id=package_version_id)

        if existing.status == 'DRAFT':
            for key, val in modifications.items():
                if hasattr(existing, key):
                    setattr(existing, key, val)
            existing.save(using=alias)
            return existing

        # For published/active versions, create a new version (Immutability guarantee)
        latest_version = PackageVersion.objects.using(alias).filter(
            package=existing.package
        ).order_by('-version_number').first()
        next_ver = latest_version.version_number + 1

        new_version = PackageVersion(
            package=existing.package,
            version_number=next_ver,
            name_snapshot=modifications.get('name_snapshot', existing.name_snapshot),
            description_snapshot=modifications.get('description_snapshot', existing.description_snapshot),
            duration_value=modifications.get('duration_value', existing.duration_value),
            duration_unit=modifications.get('duration_unit', existing.duration_unit),
            validity_days=modifications.get('validity_days', existing.validity_days),
            is_trial_package=modifications.get('is_trial_package', existing.is_trial_package),
            effective_from=modifications.get('effective_from', timezone.now()),
            effective_until=modifications.get('effective_until', None),
            status='DRAFT',
            created_by_user=actor,
        )
        new_version.save(using=alias)

        # Clone active prices
        for price in existing.prices.using(alias).filter(status='ACTIVE'):
            PackagePrice.objects.using(alias).create(
                package_version=new_version,
                branch=price.branch,
                currency=price.currency,
                base_price=price.base_price,
                tax_percent=price.tax_percent,
                effective_from=timezone.now(),
                status='ACTIVE',
                created_by_user=actor,
            )

        # Clone active entitlements
        for ent in existing.entitlement_definitions.using(alias).filter(status='ACTIVE'):
            PackageEntitlementDefinition.objects.using(alias).create(
                package_version=new_version,
                entitlement_type=ent.entitlement_type,
                allocated_units=ent.allocated_units,
                is_unlimited=ent.is_unlimited,
                validity_days=ent.validity_days,
                reference_type=ent.reference_type,
                reference_id=ent.reference_id,
                configuration=ent.configuration,
                status='ACTIVE',
            )

        record_business_audit(
            organization=existing.package.organization,
            module='core',
            action_code='PACKAGE_VERSION_CLONED',
            entity_type='PackageVersion',
            entity_id=new_version.id,
            actor_user=actor,
            event_description=f"Cloned immutable package version {existing.version_number} -> {new_version.version_number}",
            before_data={'original_version_id': str(existing.id), 'version_number': existing.version_number},
            after_data={'new_version_id': str(new_version.id), 'version_number': new_version.version_number},
            db_alias=alias,
        )
        return new_version

    @classmethod
    @transaction.atomic
    def add_package_price(
        cls,
        package_version: PackageVersion,
        base_price: Decimal,
        actor: TenantUser,
        branch: Optional[Branch] = None,
        currency: str = 'INR',
        tax_percent: Decimal = Decimal('0.000'),
        effective_from: Optional[timezone.datetime] = None,
        status: str = 'ACTIVE',
        db_alias: Optional[str] = None,
    ) -> PackagePrice:
        alias = db_alias or 'default'
        if base_price < Decimal('0.00'):
            raise ValidationError("base_price cannot be negative")

        effective_from = effective_from or timezone.now()

        price = PackagePrice(
            package_version=package_version,
            branch=branch,
            currency=currency,
            base_price=base_price,
            tax_percent=tax_percent,
            effective_from=effective_from,
            status=status,
            created_by_user=actor,
        )
        price.save(using=alias)
        return price

    @classmethod
    @transaction.atomic
    def add_entitlement_definition(
        cls,
        package_version: PackageVersion,
        entitlement_type: str,
        allocated_units: Optional[Decimal] = None,
        is_unlimited: bool = False,
        validity_days: Optional[int] = None,
        reference_type: Optional[str] = None,
        reference_id: Optional[str] = None,
        configuration: Optional[Dict[str, Any]] = None,
        status: str = 'ACTIVE',
        db_alias: Optional[str] = None,
    ) -> PackageEntitlementDefinition:
        alias = db_alias or 'default'
        if not is_unlimited and (allocated_units is None or allocated_units <= 0):
            raise ValidationError("Must specify allocated_units > 0 or set is_unlimited=True")

        ent = PackageEntitlementDefinition(
            package_version=package_version,
            entitlement_type=entitlement_type,
            allocated_units=allocated_units,
            is_unlimited=is_unlimited,
            validity_days=validity_days,
            reference_type=reference_type,
            reference_id=reference_id,
            configuration=configuration or {},
            status=status,
        )
        ent.save(using=alias)
        return ent


class TermsLegalService:
    """
    Service managing Terms Documents, version publishing, and legal acceptance records.
    """

    @classmethod
    @transaction.atomic
    def create_terms_document(
        cls,
        organization: Organization,
        code: str,
        name: str,
        document_type: str,
        status: str = 'ACTIVE',
        actor: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> TermsDocument:
        alias = db_alias or 'default'
        doc = TermsDocument(
            organization=organization,
            code=code.strip().upper(),
            name=name.strip(),
            document_type=document_type,
            status=status,
        )
        doc.save(using=alias)
        return doc

    @classmethod
    @transaction.atomic
    def create_terms_version(
        cls,
        terms_document: TermsDocument,
        content_text: str,
        created_by_user: TenantUser,
        effective_from: Optional[timezone.datetime] = None,
        status: str = 'DRAFT',
        file_id: Optional[str] = None,
        db_alias: Optional[str] = None,
    ) -> TermsDocumentVersion:
        alias = db_alias or 'default'
        latest = TermsDocumentVersion.objects.using(alias).filter(
            terms_document=terms_document
        ).order_by('-version_number').first()
        next_ver = (latest.version_number + 1) if latest else 1

        effective_from = effective_from or timezone.now()

        version = TermsDocumentVersion(
            terms_document=terms_document,
            version_number=next_ver,
            content_text=content_text,
            file_id=file_id,
            effective_from=effective_from,
            status=status,
            created_by_user=created_by_user,
        )
        version.save(using=alias)
        return version

    @classmethod
    @transaction.atomic
    def publish_terms_version(
        cls,
        terms_document_version_id: str,
        actor: TenantUser,
        db_alias: Optional[str] = None,
    ) -> TermsDocumentVersion:
        alias = db_alias or 'default'
        version = TermsDocumentVersion.objects.using(alias).select_for_update().get(id=terms_document_version_id)
        now = timezone.now()

        # Retire prior active versions
        prior_active = TermsDocumentVersion.objects.using(alias).filter(
            terms_document=version.terms_document,
            status='ACTIVE'
        ).exclude(id=version.id)

        for prior in prior_active:
            prior.status = 'RETIRED'
            prior.effective_until = now
            prior.save(using=alias, update_fields=['status', 'effective_until', 'updated_at'])

        version.status = 'ACTIVE'
        version.effective_from = now
        version.save(using=alias, update_fields=['status', 'effective_from', 'updated_at'])

        record_business_audit(
            organization=version.terms_document.organization,
            module='core',
            action_code='TERMS_VERSION_PUBLISHED',
            entity_type='TermsDocumentVersion',
            entity_id=version.id,
            actor_user=actor,
            event_description=f"Published terms version {version.terms_document.code} v{version.version_number}",
            after_data={'status': 'ACTIVE', 'effective_from': version.effective_from.isoformat()},
            db_alias=alias,
        )
        return version

    @classmethod
    @transaction.atomic
    def record_terms_acceptance(
        cls,
        terms_document_version_id: str,
        accepted_via: str,
        lead_id: Optional[str] = None,
        user_profile_id: Optional[str] = None,
        order_id: Optional[str] = None,
        ip_address: Optional[str] = None,
        device_metadata: Optional[Dict[str, Any]] = None,
        db_alias: Optional[str] = None,
    ) -> TermsAcceptance:
        alias = db_alias or 'default'
        if not lead_id and not user_profile_id:
            raise ValidationError("Terms acceptance requires lead_id or user_profile_id")

        version = TermsDocumentVersion.objects.using(alias).get(id=terms_document_version_id)
        if version.status != 'ACTIVE':
            raise ValidationError(f"Cannot accept terms version with status {version.status}; must be ACTIVE")

        now = timezone.now()
        acceptance = TermsAcceptance(
            terms_document_version=version,
            lead_id=lead_id,
            user_profile_id=user_profile_id,
            order_id=order_id,
            accepted_at=now,
            accepted_via=accepted_via,
            ip_address=ip_address,
            device_metadata=device_metadata or {},
        )
        acceptance.save(using=alias)

        subject = f"User {user_profile_id}" if user_profile_id else f"Lead {lead_id}"
        record_business_audit(
            organization=version.terms_document.organization,
            module='core',
            action_code='TERMS_ACCEPTED',
            entity_type='TermsAcceptance',
            entity_id=acceptance.id,
            actor_user=None,
            event_description=f"{subject} accepted terms {version}",
            after_data={'version_id': str(version.id), 'accepted_via': accepted_via},
            db_alias=alias,
        )
        return acceptance
