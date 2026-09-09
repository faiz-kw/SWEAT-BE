"""
Views for Fitness Assessments, Exercise Library, Workout Programs, and Nutrition Plans.
Enforces strict Tenant isolation across all operations.
"""

from rest_framework import viewsets, permissions, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from django.utils import timezone
from drf_spectacular.utils import extend_schema, OpenApiParameter
import uuid

from .models import (
    FitnessAssessment,
    Exercise,
    WorkoutProgram,
    WorkoutDay,
    WorkoutDayExercise,
    FoodItem,
    NutritionPlan,
    MealItem,
)
from .serializers import (
    FitnessAssessmentSerializer,
    ExerciseSerializer,
    WorkoutProgramSerializer,
    WorkoutProgramDetailSerializer,
    FoodItemSerializer,
    NutritionPlanSerializer,
    NutritionPlanDetailSerializer,
)

# ---------------------------------------------------------------------------
# 1. Assessments
# ---------------------------------------------------------------------------

class FitnessAssessmentViewSet(viewsets.ModelViewSet):
    """
    CRUD ViewSet for Member Fitness & Mobility Assessments.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = FitnessAssessmentSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['member__name', 'trainer_summary']
    ordering_fields = ['assessment_date', 'overall_fitness_score']
    ordering = ['-assessment_date']

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser and not user.tenant:
            qs = FitnessAssessment.objects.all()
        else:
            qs = FitnessAssessment.objects.filter(tenant=user.tenant)

        member_id = self.request.query_params.get('member')
        if member_id:
            qs = qs.filter(member_id=member_id)
        return qs.select_related('member', 'assessed_by', 'tenant')

    def perform_create(self, serializer):
        tenant = self.request.user.tenant
        asm_id = serializer.validated_data.get('id')
        if not asm_id:
            asm_id = f"ASM-{uuid.uuid4().hex[:6].upper()}"

        height = serializer.validated_data.get('height_cm')
        weight = serializer.validated_data.get('weight_kg')
        bmi = serializer.validated_data.get('bmi')

        # Auto calculate BMI if height and weight are provided and BMI is empty
        if not bmi and height and weight and height > 0:
            h_meters = float(height) / 100.0
            bmi = round(float(weight) / (h_meters * h_meters), 1)

        serializer.save(
            id=asm_id,
            tenant=tenant,
            assessed_by=self.request.user if not serializer.validated_data.get('assessed_by') else serializer.validated_data.get('assessed_by'),
            bmi=bmi
        )

    @extend_schema(
        summary="Get Member Assessment Progress Timeline",
        description="Returns all historical assessments for a member sorted chronologically.",
        responses={200: FitnessAssessmentSerializer(many=True)}
    )
    @action(detail=False, methods=['get'], url_path=r'member/(?P<member_id>[^/.]+)')
    def member_history(self, request, member_id=None):
        tenant = request.user.tenant
        qs = FitnessAssessment.objects.filter(member_id=member_id)
        if tenant:
            qs = qs.filter(tenant=tenant)
        serializer = FitnessAssessmentSerializer(qs.order_by('assessment_date'), many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# 2. Exercise Library
# ---------------------------------------------------------------------------

class ExerciseViewSet(viewsets.ModelViewSet):
    """
    CRUD ViewSet for the Centralized Exercise Library.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ExerciseSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'instructions', 'equipment']
    ordering_fields = ['name', 'category', 'difficulty']
    ordering = ['name']

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser and not user.tenant:
            qs = Exercise.objects.all()
        else:
            qs = Exercise.objects.filter(tenant=user.tenant)

        category = self.request.query_params.get('category')
        if category:
            qs = qs.filter(category=category)

        muscle = self.request.query_params.get('muscle')
        if muscle:
            qs = qs.filter(primary_muscle=muscle)

        difficulty = self.request.query_params.get('difficulty')
        if difficulty:
            qs = qs.filter(difficulty=difficulty)

        equipment = self.request.query_params.get('equipment')
        if equipment:
            qs = qs.filter(equipment__icontains=equipment)

        return qs

    def perform_create(self, serializer):
        tenant = self.request.user.tenant
        exe_id = serializer.validated_data.get('id')
        if not exe_id:
            exe_id = f"EXE-{uuid.uuid4().hex[:6].upper()}"
        serializer.save(id=exe_id, tenant=tenant)


# ---------------------------------------------------------------------------
# 3. Workout Programs
# ---------------------------------------------------------------------------

class WorkoutProgramViewSet(viewsets.ModelViewSet):
    """
    CRUD ViewSet for Workout Programs and Training Splits.
    """
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'member__name', 'goal']
    ordering_fields = ['start_date', 'name']
    ordering = ['-start_date']

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser and not user.tenant:
            qs = WorkoutProgram.objects.all()
        else:
            qs = WorkoutProgram.objects.filter(tenant=user.tenant)

        member_id = self.request.query_params.get('member')
        if member_id:
            qs = qs.filter(member_id=member_id)

        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            qs = qs.filter(is_active=(is_active.lower() == 'true'))

        return qs.select_related('member', 'trainer', 'tenant').prefetch_related('workout_days__exercises__exercise')

    def get_serializer_class(self):
        if self.action in ['retrieve']:
            return WorkoutProgramDetailSerializer
        return WorkoutProgramSerializer

    def perform_create(self, serializer):
        tenant = self.request.user.tenant
        prg_id = serializer.validated_data.get('id')
        if not prg_id:
            prg_id = f"PRG-{uuid.uuid4().hex[:6].upper()}"
        serializer.save(id=prg_id, tenant=tenant)


# ---------------------------------------------------------------------------
# 4. Indian Food Database & Nutrition Plans
# ---------------------------------------------------------------------------

class FoodItemViewSet(viewsets.ModelViewSet):
    """
    CRUD ViewSet for Indian Regional Food Database & Macronutrients.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = FoodItemSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'serving_unit']
    ordering_fields = ['calories', 'protein_g', 'name']
    ordering = ['name']

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser and not user.tenant:
            qs = FoodItem.objects.all()
        else:
            qs = FoodItem.objects.filter(tenant=user.tenant)

        diet_type = self.request.query_params.get('diet_type')
        if diet_type:
            qs = qs.filter(diet_type=diet_type)
        return qs

    def perform_create(self, serializer):
        tenant = self.request.user.tenant
        food_id = serializer.validated_data.get('id')
        if not food_id:
            food_id = f"FOD-{uuid.uuid4().hex[:6].upper()}"
        serializer.save(id=food_id, tenant=tenant)


class NutritionPlanViewSet(viewsets.ModelViewSet):
    """
    CRUD ViewSet for Personalized Nutrition Plans & Meal Schedules.
    """
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'member__name', 'notes']
    ordering_fields = ['created_at', 'daily_calorie_target']
    ordering = ['-created_at']

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser and not user.tenant:
            qs = NutritionPlan.objects.all()
        else:
            qs = NutritionPlan.objects.filter(tenant=user.tenant)

        member_id = self.request.query_params.get('member')
        if member_id:
            qs = qs.filter(member_id=member_id)

        diet_type = self.request.query_params.get('diet_type')
        if diet_type:
            qs = qs.filter(diet_type=diet_type)

        return qs.select_related('member', 'trainer', 'tenant').prefetch_related('meals__food_item')

    def get_serializer_class(self):
        if self.action in ['retrieve']:
            return NutritionPlanDetailSerializer
        return NutritionPlanSerializer

    def perform_create(self, serializer):
        tenant = self.request.user.tenant
        nut_id = serializer.validated_data.get('id')
        if not nut_id:
            nut_id = f"NUT-{uuid.uuid4().hex[:6].upper()}"
        serializer.save(id=nut_id, tenant=tenant)
