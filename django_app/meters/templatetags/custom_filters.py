from django import template

register = template.Library()

@register.filter
def get_item(dictionary, key):
    return dictionary.get(key)

@register.filter
def attr(obj, field):
    return getattr(obj, field, '')

@register.filter
def progress_class(value):
    """
    Возвращает CSS-класс для прогресс-бара в зависимости от процента.
    """
    try:
        percent = float(value)
    except (ValueError, TypeError):
        return 'bg-secondary-pastel'
    
    if percent >= 80:
        return 'bg-success-pastel'
    elif percent >= 50:
        return 'bg-warning-pastel'
    else:
        return 'bg-danger-pastel'
    
@register.filter
def progress_style(value):
    """
    Возвращает строку style для прогресс-бара: width: X%;
    """
    try:
        percent = float(value)
    except (ValueError, TypeError):
        percent = 0
    return f"width: {int(percent)}%;"

@register.filter
def make_list(value):
    return range(value)

@register.filter
def getattr(obj, attr):
    """Возвращает значение атрибута объекта по строке."""
    try:
        return getattr(obj, attr, '')
    except:
        return ''

@register.filter
def split(value, arg):
    return value.split(arg)

@register.filter
def get_item(dictionary, key):
    return dictionary.get(key)

@register.filter
def multiply(value, arg):
    try:
        return float(value) * float(arg)
    except (ValueError, TypeError):
        return 0