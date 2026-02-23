{
    'name': 'SAI - Reportes de Stock con Datos Ambientales',
    'version': '19.0.1.1.0',
    'category': 'Inventory',
    'summary': 'Extiende reportes de Ubicaciones e Historial de Movimientos con todos los campos del registro de manifiestos de residuos peligrosos',
    'author': 'Alphaqueb Consulting',
    'website': 'https://alphaqueb.com',
    'depends': [
        'stock',
        'residuo_recepcion_sai',
        'manifiesto_ambiental',
    ],
    'data': [
        'reports/report_location_residuos.xml',
        'reports/report_stock_moves_residuos.xml',
        'views/stock_report_views.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
