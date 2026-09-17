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
        program_type: Any = 'MEMBERSHIP',
        category_id: Optional[str] = None,
        description: Optional[str] = None,
        trial_allowed: bool = False,
        status: str = 'ACTIVE',
        actor: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
    ) -> Program:
        alias = db_alias or 'default'
        from .models_catalog import ProgramType

        pt_instance = None
        legacy_code = None
        if isinstance(program_type, ProgramType):
            pt_instance = program_type
            legacy_code = pt_instance.code
        elif program_type:
            try:
                pt_id = uuid.UUID(str(program_type))
                pt_instance = ProgramType.objects.using(alias).filter(id=pt_id).first()
                if pt_instance:
                    legacy_code = pt_instance.code
            except (ValueError, TypeError):
                code_str = str(program_type).upper().strip()
                legacy_code = code_str
                pt_instance, _ = ProgramType.objects.using(alias).get_or_create(
                    organization=organization,
                    code=code_str,
                    defaults={'name': code_str.replace('_', ' ').title(), 'status': 'ACTIVE'}
                )

        program = Program(
            organization=organization,
            code=code.strip().upper(),
            name=name.strip(),
            program_type=pt_instance,
            legacy_program_type=legacy_code,
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
            after_data={'code': program.code, 'name': program.name, 'program_type': legacy_code or (pt_instance.code if pt_instance else None)},
            db_alias=alias,
        )
        return program

    @classmethod
    @transaction.atomic
    def update_program(
        cls,
        program: Program,
        actor: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
        **updates,
    ) -> Program:
        alias = db_alias or 'default'
        from .models_catalog import ProgramType

        before_data = {'name': program.name, 'status': program.status}
        for field, val in updates.items():
            if field == 'program_type' and val:
                if isinstance(val, ProgramType):
                    program.program_type = val
                    program.legacy_program_type = val.code
                else:
                    try:
                        pt_id = uuid.UUID(str(val))
                        pt = ProgramType.objects.using(alias).filter(id=pt_id).first()
                        if pt:
                            program.program_type = pt
                            program.legacy_program_type = pt.code
                    except (ValueError, TypeError):
                        code_str = str(val).upper().strip()
                        pt, _ = ProgramType.objects.using(alias).get_or_create(
                            organization=program.organization,
                            code=code_str,
                            defaults={'name': code_str.replace('_', ' ').title(), 'status': 'ACTIVE'}
                        )
                        program.program_type = pt
                        program.legacy_program_type = code_str
            elif hasattr(program, field):
                setattr(program, field, val)

        program.save(using=alias)
        record_business_audit(
            organization=program.organization,
            module='core',
            action_code='PROGRAM_UPDATED',
            entity_type='Program',
            entity_id=program.id,
            actor_user=actor,
            event_description=f"Updated program {program.name} ({program.code})",
            before_data=before_data,
            after_data={'name': program.name, 'status': program.status},
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
        program = None
        if program_id:
            program = Program.objects.using(alias).filter(id=program_id).first()
            if not program:
                raise ValidationError(f"Program with id '{program_id}' does not exist.")
        else:
            program = Program.objects.using(alias).filter(organization=organization).first()
            if not program:
                program = cls.create_program(
                    organization=organization,
                    code='DEFAULT',
                    name='Default Program',
                    status='ACTIVE',
                    db_alias=alias,
                )

        package = Package(
            organization=organization,
            program=program,
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
            event_description=f"Created package {package.name} ({package.code}) under program {program.code}",
            after_data={'code': package.code, 'name': package.name, 'program_id': str(program.id)},
            db_alias=alias,
        )
        return package

    @classmethod
    @transaction.atomic
    def update_package(
        cls,
        package: Package,
        actor: Optional[TenantUser] = None,
        db_alias: Optional[str] = None,
        **updates,
    ) -> Package:
        alias = db_alias or 'default'
        before_data = {'name': package.name, 'status': package.status}
        for field, val in updates.items():
            if field == 'program_id' and val:
                package.program_id = val
            elif hasattr(package, field):
                setattr(package, field, val)
        package.save(using=alias)

        record_business_audit(
            organization=package.organization,
            module='core',
            action_code='PACKAGE_UPDATED',
            entity_type='Package',
            entity_id=package.id,
            actor_user=actor,
            event_description=f"Updated package {package.name} ({package.code})",
            before_data=before_data,
            after_data={'name': package.name, 'status': package.status},
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
        total_days: Optional[int] = None,
        description_snapshot: Optional[str] = None,
        validity_days: Optional[int] = None,
        is_trial_package: bool = False,
        is_trial: bool = False,
        only_for_trial: bool = False,
        show_on_web: bool = True,
        show_on_app: bool = True,
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
        trial_flag = is_trial or is_trial_package

        if not total_days:
            if duration_unit == 'DAY':
                total_days = duration_value
            elif duration_unit == 'WEEK':
                total_days = duration_value * 7
            elif duration_unit == 'MONTH':
                total_days = duration_value * 30
            elif duration_unit == 'YEAR':
                total_days = duration_value * 365
            else:
                total_days = 30

        pkg_version = PackageVersion(
            package=package,
            version_number=next_ver,
            name_snapshot=name_snapshot.strip(),
            description_snapshot=description_snapshot,
            duration_value=duration_value,
            duration_unit=duration_unit,
            total_days=total_days,
            validity_days=validity_days,
            is_trial_package=trial_flag,
            is_trial=trial_flag,
            only_for_trial=only_for_trial,
            show_on_web=show_on_web,
            show_on_app=show_on_app,
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
    def publish_package_version(
        cls,
        package_version_id: str,
        actor: TenantUser,
        effective_from: Optional[timezone.datetime] = None,
        db_alias: Optional[str] = None,
    ) -> PackageVersion:
        alias = db_alias or 'default'
        with transaction.atomic(using=alias):
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
                record_business_audit(
                    organization=prior.package.organization,
                    module='core',
                    action_code='PACKAGE_VERSION_RETIRED',
                    entity_type='PackageVersion',
                    entity_id=prior.id,
                    actor_user=actor,
                    event_description=f"Retired prior package version {prior}",
                    after_data={'status': 'RETIRED', 'effective_until': now.isoformat()},
                    db_alias=alias,
                )

            pkg_ver.status = 'ACTIVE'
            pkg_ver.published_at = now
            pkg_ver.effective_from = effective_from or now
            pkg_ver.save(using=alias, update_fields=['status', 'published_at', 'effective_from', 'updated_at'])

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
    def modify_package_version_safely(
        cls,
        package_version_id: str,
        actor: TenantUser,
        modifications: Dict[str, Any],
        db_alias: Optional[str] = None,
    ) -> PackageVersion:
        alias = db_alias or 'default'
        with transaction.atomic(using=alias):
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

            trial_flag = modifications.get('is_trial', modifications.get('is_trial_package', existing.is_trial))

            new_version = PackageVersion(
                package=existing.package,
                version_number=next_ver,
                name_snapshot=modifications.get('name_snapshot', existing.name_snapshot),
                description_snapshot=modifications.get('description_snapshot', existing.description_snapshot),
                duration_value=modifications.get('duration_value', existing.duration_value),
                duration_unit=modifications.get('duration_unit', existing.duration_unit),
                total_days=modifications.get('total_days', existing.total_days),
                validity_days=modifications.get('validity_days', existing.validity_days),
                is_trial_package=trial_flag,
                is_trial=trial_flag,
                only_for_trial=modifications.get('only_for_trial', existing.only_for_trial),
                show_on_web=modifications.get('show_on_web', existing.show_on_web),
                show_on_app=modifications.get('show_on_app', existing.show_on_app),
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
                    display_price=price.display_price,
                    prices_include_tax=price.prices_include_tax,
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
                    extra_unit_price=ent.extra_unit_price,
                    validity_days=ent.validity_days,
                    reference_type=ent.reference_type,
                    reference_id=ent.reference_id,
                    configuration=ent.configuration,
                    status='ACTIVE',
                )

            # Clone active class access rules
            for rule in existing.class_access_rules.using(alias).filter(status='ACTIVE'):
                from .models_classes import PackageClassAccessRule
                PackageClassAccessRule.objects.using(alias).create(
                    package_version=new_version,
                    class_template=rule.class_template,
                    class_category=rule.class_category,
                    branch=rule.branch,
                    access_type=rule.access_type,
                    entitlement_type=rule.entitlement_type,
                    units_per_booking=rule.units_per_booking,
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
        branch_id: Optional[str] = None,
        currency: str = 'INR',
        display_price: Optional[Decimal] = None,
        prices_include_tax: bool = False,
        tax_percent: Decimal = Decimal('0.000'),
        effective_from: Optional[timezone.datetime] = None,
        status: str = 'ACTIVE',
        db_alias: Optional[str] = None,
    ) -> PackagePrice:
        alias = db_alias or 'default'
        if branch is None and branch_id:
            branch = Branch.objects.using(alias).filter(id=branch_id).first()
        if base_price < Decimal('0.00'):
            raise ValidationError("base_price cannot be negative")

        effective_from = effective_from or timezone.now()

        price = PackagePrice(
            package_version=package_version,
            branch=branch,
            currency=currency,
            base_price=base_price,
            display_price=display_price,
            prices_include_tax=prices_include_tax,
            tax_percent=tax_percent,
            effective_from=effective_from,
            status=status,
            created_by_user=actor,
        )
        price.save(using=alias)

        record_business_audit(
            organization=package_version.package.organization,
            module='core',
            action_code='PACKAGE_PRICE_CREATED',
            entity_type='PackagePrice',
            entity_id=price.id,
            actor_user=actor,
            event_description=f"Created price for {package_version}: {currency} {base_price}",
            after_data={'base_price': str(base_price), 'currency': currency, 'status': status},
            db_alias=alias,
        )
        return price

    @classmethod
    @transaction.atomic
    def add_entitlement_definition(
        cls,
        package_version: PackageVersion,
        entitlement_type: str,
        allocated_units: Optional[Decimal] = None,
        is_unlimited: bool = False,
        extra_unit_price: Decimal = Decimal('0.00'),
        validity_days: Optional[int] = None,
        reference_type: Optional[str] = None,
        reference_id: Optional[str] = None,
        configuration: Optional[Dict[str, Any]] = None,
        status: str = 'ACTIVE',
        actor: Optional[TenantUser] = None,
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
            extra_unit_price=extra_unit_price,
            validity_days=validity_days,
            reference_type=reference_type,
            reference_id=reference_id,
            configuration=configuration or {},
            status=status,
        )
        ent.save(using=alias)

        record_business_audit(
            organization=package_version.package.organization,
            module='core',
            action_code='PACKAGE_ENTITLEMENT_CREATED',
            entity_type='PackageEntitlementDefinition',
            entity_id=ent.id,
            actor_user=actor,
            event_description=f"Created entitlement {entitlement_type} for {package_version}",
            after_data={'entitlement_type': entitlement_type, 'allocated_units': str(allocated_units) if allocated_units else 'UNLIMITED'},
            db_alias=alias,
        )
        return ent

    create_package_price = add_package_price
    create_entitlement_definition = add_entitlement_definition
    clone_modify_package_version = modify_package_version_safely


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
