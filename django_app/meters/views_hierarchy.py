"""Иерархия сети: районы, подстанции, фидеры, ТП; привязка счётчиков к ТП.

Вынесено из views.py. Маршруты подключены в urls.py через `views_hierarchy`.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404

from .models import Device, Region, Substation, Feeder, TransformerSubstation
from .views import is_allowed_user


# Главная страница иерархии
@login_required
@user_passes_test(is_allowed_user)
def hierarchy_management(request):
    """Страница управления иерархией в виде дерева."""
    tree = get_hierarchy_tree()
    context = {
        'tree': tree,
    }
    return render(request, 'meters/hierarchy.html', context)


def get_hierarchy_tree():
    tree = []
    regions = Region.objects.all().order_by('name')
    for region in regions:
        region_node = {
            'id': region.id,
            'name': region.name,
            'type': 'region',
            'children': []
        }
        substations = region.substations.all().order_by('name')
        for substation in substations:
            substation_node = {
                'id': substation.id,
                'name': substation.name,
                'type': 'substation',
                'children': []
            }
            # Вводные фидеры (parent_feeder is null, feeder_type='input')
            input_feeders = substation.feeders.filter(parent_feeder__isnull=True, feeder_type='input').order_by('name')
            for feeder in input_feeders:
                feeder_node = {
                    'id': feeder.id,
                    'name': feeder.name,
                    'type': 'feeder',
                    'feeder_type': feeder.get_feeder_type_display(),
                    'head_meter': feeder.head_meter.serial_number if feeder.head_meter else None,
                    'children': []
                }
                # Отходящие фидеры (parent_feeder = this feeder)
                output_feeders = feeder.children.all().order_by('name')
                for out_feeder in output_feeders:
                    out_feeder_node = {
                        'id': out_feeder.id,
                        'name': out_feeder.name,
                        'type': 'feeder',
                        'feeder_type': out_feeder.get_feeder_type_display(),
                        'head_meter': out_feeder.head_meter.serial_number if out_feeder.head_meter else None,
                        'children': []
                    }
                    # ТП привязаны к отходящему фидеру (или можно к любому, но обычно к отходящему)
                    tps = out_feeder.tps.all().order_by('name')
                    for tp in tps:
                        tp_node = {
                            'id': tp.id,
                            'name': tp.name,
                            'type': 'tp',
                            'head_meter': tp.head_meter.serial_number if tp.head_meter else None,
                            'children': []
                        }
                        out_feeder_node['children'].append(tp_node)
                    feeder_node['children'].append(out_feeder_node)
                substation_node['children'].append(feeder_node)
            region_node['children'].append(substation_node)
        tree.append(region_node)
    return tree


@login_required
@user_passes_test(is_allowed_user)
def hierarchy_node_details(request):
    node_id = request.GET.get('id')
    node_type = request.GET.get('type')
    
    context = {}
    if node_type == 'region':
        obj = get_object_or_404(Region, id=node_id)
        context['obj'] = obj
        context['type'] = 'region'
        context['label'] = 'Район'
        context['parent_label'] = None
        context['parent_choices'] = []
        context['feeder_type_choices'] = []
        context['show_head_meter'] = False  # у района нет головного
    elif node_type == 'substation':
        obj = get_object_or_404(Substation, id=node_id)
        context['obj'] = obj
        context['type'] = 'substation'
        context['label'] = 'Подстанция'
        context['parent_label'] = 'Район'
        parents = Region.objects.all().order_by('name')
        context['parent_choices'] = [(p.id, p.name, p.id == obj.region_id) for p in parents]
        context['feeder_type_choices'] = []
        context['show_head_meter'] = False  # у подстанции нет головного
    elif node_type == 'feeder':
        obj = get_object_or_404(Feeder, id=node_id)
        context['obj'] = obj
        context['type'] = 'feeder'
        context['label'] = 'Фидер'
        # Если фидер вводной, то родитель — подстанция
        if obj.feeder_type == 'input':
            context['parent_label'] = 'Подстанция'
            parents = Substation.objects.all().order_by('name')
            context['parent_choices'] = [(p.id, p.name, p.id == obj.substation_id) for p in parents]
            context['parent_field'] = 'substation_id'  # для формы
            context['show_parent_feeder'] = False
        else:  # отходящий
            context['parent_label'] = 'Вводной фидер'
            # Список вводных фидеров той же подстанции
            input_feeders = Feeder.objects.filter(substation=obj.substation, feeder_type='input', parent_feeder__isnull=True).order_by('name')
            context['parent_choices'] = [(f.id, f.name, f.id == obj.parent_feeder_id) for f in input_feeders]
            context['parent_field'] = 'parent_feeder_id'
            context['show_parent_feeder'] = True
        context['feeder_type_choices'] = [
            ('input', 'Вводной', obj.feeder_type == 'input'),
            ('output', 'Отходящий', obj.feeder_type == 'output'),
        ]
        context['show_head_meter'] = True  # у фидера есть головной
    elif node_type == 'tp':
        obj = get_object_or_404(TransformerSubstation, id=node_id)
        context['obj'] = obj
        context['type'] = 'tp'
        context['label'] = 'ТП'
        context['parent_label'] = 'Фидер'
        parents = Feeder.objects.all().order_by('name')
        context['parent_choices'] = [(p.id, p.name, p.id == obj.feeder_id) for p in parents]
        context['feeder_type_choices'] = []
        context['show_head_meter'] = True  # у ТП может быть головной
    else:
        return HttpResponse('<p class="text-muted">Неизвестный тип</p>')
    
    context['devices'] = Device.objects.filter(status='active').order_by('serial_number')
    return render(request, 'meters/_node_details.html', context)


# управления иерархией: районы → подстанции → фидеры → ТП с возможностью назначения головных счётчиков и массовой привязки устройств к ТП
# AJAX-таблица для вкладок  
# удалить потом
@login_required
@user_passes_test(is_allowed_user)
def hierarchy_table(request):
    level = request.GET.get('level', 'region')
    devices = Device.objects.filter(status='active').order_by('serial_number')

    if level == 'region':
        objects = Region.objects.all().order_by('name')
        columns = ['Название']
        fields = ['name']
        parent_field = None
    elif level == 'substation':
        objects = Substation.objects.select_related('region').all().order_by('name')
        columns = ['Название', 'Район']
        fields = ['name', 'region']
        parent_field = 'region'
        # head_meter убрали
    elif level == 'feeder':
        objects = Feeder.objects.select_related('substation').all().order_by('name')
        columns = ['Название', 'Подстанция', 'Тип', 'Головной счётчик']
        fields = ['name', 'substation', 'feeder_type', 'head_meter']
        parent_field = 'substation'
    elif level == 'tp':
        objects = TransformerSubstation.objects.select_related('feeder').all().order_by('name')
        columns = ['Название', 'Фидер', 'Головной счётчик']
        fields = ['name', 'feeder', 'head_meter']
        parent_field = 'feeder'
    else:
        objects = []
        columns = []
        fields = []
        parent_field = None

    context = {
        'level': level,
        'objects': objects,
        'columns': columns,
        'fields': fields,
        'parent_field': parent_field,
        'devices': devices,
    }
    return render(request, 'meters/_hierarchy_table.html', context)


# Добавление/редактирование объекта (модальное окно)
@login_required
@user_passes_test(is_allowed_user)
def hierarchy_add(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    level = request.POST.get('level')
    name = request.POST.get('name', '').strip()
    head_meter_id = request.POST.get('head_meter_id')
    parent_id = request.POST.get('parent_id')
    feeder_type = request.POST.get('feeder_type')  # для фидера

    if not name:
        return JsonResponse({'error': 'Название обязательно'}, status=400)

    try:
        if level == 'region':
            obj = Region.objects.create(name=name)
        elif level == 'substation':
            if not parent_id:
                return JsonResponse({'error': 'Выберите район'}, status=400)
            obj = Substation.objects.create(
                name=name,
                region_id=parent_id,
            )
        elif level == 'feeder':
            if not parent_id:
                return JsonResponse({'error': 'Выберите подстанцию'}, status=400)
            if not feeder_type:
                return JsonResponse({'error': 'Выберите тип фидера'}, status=400)
            obj = Feeder.objects.create(
                name=name,
                substation_id=parent_id,
                feeder_type=feeder_type,
                head_meter_id=head_meter_id or None
            )
        elif level == 'tp':
            if not parent_id:
                return JsonResponse({'error': 'Выберите фидер'}, status=400)
            obj = TransformerSubstation.objects.create(
                name=name,
                feeder_id=parent_id,
                head_meter_id=head_meter_id or None
            )
        else:
            return JsonResponse({'error': 'Неверный уровень'}, status=400)
        return JsonResponse({'success': True, 'id': obj.id, 'name': obj.name})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@login_required
@user_passes_test(is_allowed_user)
def hierarchy_edit(request, pk):
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    level = request.POST.get('level')
    name = request.POST.get('name', '').strip()
    head_meter_id = request.POST.get('head_meter_id')
    parent_id = request.POST.get('parent_id')
    feeder_type = request.POST.get('feeder_type')

    if not name:
        return JsonResponse({'error': 'Название обязательно'}, status=400)

    try:
        if level == 'region':
            obj = get_object_or_404(Region, id=pk)
            obj.name = name
            obj.save()

        elif level == 'substation':
            if not parent_id:
                return JsonResponse({'error': 'Выберите район'}, status=400)
            obj = get_object_or_404(Substation, id=pk)
            obj.name = name
            obj.region_id = parent_id
            obj.save()

        elif level == 'feeder':
            if not parent_id:
                return JsonResponse({'error': 'Выберите подстанцию'}, status=400)
            if not feeder_type:
                return JsonResponse({'error': 'Выберите тип фидера'}, status=400)
            obj = get_object_or_404(Feeder, id=pk)
            obj.name = name
            obj.substation_id = parent_id
            obj.feeder_type = feeder_type
            obj.head_meter_id = head_meter_id or None
            obj.save()

        elif level == 'tp':
            if not parent_id:
                return JsonResponse({'error': 'Выберите фидер'}, status=400)
            obj = get_object_or_404(TransformerSubstation, id=pk)
            obj.name = name
            obj.feeder_id = parent_id
            obj.head_meter_id = head_meter_id or None
            obj.save()

        else:
            return JsonResponse({'error': 'Неверный уровень'}, status=400)

        return JsonResponse({'success': True})

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# Удаление объекта
@login_required
@user_passes_test(is_allowed_user)
def hierarchy_delete(request, pk):
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    level = request.POST.get('level')
    try:
        if level == 'region':
            obj = get_object_or_404(Region, id=pk)
        elif level == 'substation':
            obj = get_object_or_404(Substation, id=pk)
        elif level == 'feeder':
            obj = get_object_or_404(Feeder, id=pk)
        elif level == 'tp':
            obj = get_object_or_404(TransformerSubstation, id=pk)
        else:
            return JsonResponse({'error': 'Неверный уровень'}, status=400)
        obj.delete()
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# Массовая привязка устройств к ТП
@login_required
@user_passes_test(is_allowed_user)
def bulk_assign_tp(request):
    if request.method == 'POST':
        serials_text = request.POST.get('serials', '').strip()
        tp_id = request.POST.get('tp_id')
        if not serials_text:
            messages.error(request, 'Список серийных номеров пуст.')
            return redirect('bulk_assign_tp')
        if not tp_id:
            messages.error(request, 'Выберите ТП.')
            return redirect('bulk_assign_tp')
        serials = [s.strip() for s in serials_text.split('\n') if s.strip()]
        updated = 0
        not_found = []
        for sn in serials:
            device = Device.objects.filter(serial_number=sn).first()
            if device:
                device.tp_id = tp_id
                device.save()
                updated += 1
            else:
                not_found.append(sn)
        if updated:
            messages.success(request, f'Обновлено устройств: {updated}')
        if not_found:
            messages.warning(request, f'Не найдены: {", ".join(not_found[:10])}')
        return redirect('bulk_assign_tp')

    # GET – показываем форму
    tps = TransformerSubstation.objects.select_related('feeder__substation__region').all().order_by('name')
    context = {'tps': tps}
    return render(request, 'meters/bulk_assign_tp.html', context)


@login_required
@user_passes_test(is_allowed_user)
def hierarchy_parents(request):
    """Возвращает список родительских объектов для указанного уровня в формате JSON."""
    level = request.GET.get('level')
    if level == 'region':
        items = Region.objects.all().order_by('name')
        data = [{'id': r.id, 'name': r.name} for r in items]
    elif level == 'substation':
        items = Substation.objects.all().order_by('name')
        data = [{'id': s.id, 'name': s.name} for s in items]
    elif level == 'feeder':
        items = Feeder.objects.all().order_by('name')
        data = [{'id': f.id, 'name': f.name} for f in items]
    else:
        return JsonResponse([], safe=False)
    return JsonResponse(data, safe=False)
