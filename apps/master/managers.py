"""
Master DB — PlatformUser Manager
"""

from django.contrib.auth.models import BaseUserManager


class PlatformUserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError('Platform users must have an email address')
        email = self.normalize_email(email)
        extra_fields.setdefault('status', 'ACTIVE')
        extra_fields.setdefault('is_active', True)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('status', 'ACTIVE')
        extra_fields.setdefault('is_active', True)
        return self.create_user(email, password, **extra_fields)
