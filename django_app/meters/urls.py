from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('devices/', views.devices, name='devices'),
    path('devices/edit/<str:serial_number>/', views.edit_device, name='edit_device'),
    path('devices/delete/<str:serial_number>/', views.delete_device, name='delete_device'),
    path('readings/', views.readings, name='readings'),
    path('bulk-readings/', views.bulk_readings, name='bulk_readings'),
    path('models/', views.models, name='models'),
    path('models/add/', views.add_model, name='add_model'),
    path('models/edit/<int:model_id>/', views.edit_model, name='edit_model'),
    path('models/delete/<int:model_id>/', views.delete_model, name='delete_model'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('producer-stats/', views.producer_stats_table, name='producer_stats_table'),
    path('devices/add/', views.add_device, name='add_device'),
    path('devices/upload/', views.upload_devices, name='upload_devices'),
    path('devices/template/', views.download_template, name='download_template'),
    path('missing-readings/', views.missing_readings_report, name='missing_readings'),
    path('devices/bulk-status/', views.bulk_update_status_page, name='bulk_update_status_page'),
    path('import-readings-excel/', views.import_readings_excel, name='import_readings_excel'),
    path('restart-robot/<str:robot_name>/', views.restart_robot, name='restart_robot'),
    # path('balance/', views.balance_report, name='balance_report'),
    # path('directories/', views.directories, name='directories'),
    # path('directories-table/', views.directories_table, name='directories_table'),
]