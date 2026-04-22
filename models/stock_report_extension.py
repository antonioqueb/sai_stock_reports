# -*- coding: utf-8 -*-
"""
Extensión de stock.quant y stock.move.line para exponer todos los campos
del registro de manifiestos de residuos peligrosos SAI.

Soporte para:
- Manifiestos de ENTRADA (recepción): el picking es creado por residuo.recepcion
  cuyo `name` se guarda en `picking.origin`. Desde ahí se llega al manifiesto.
- Manifiestos de SALIDA: el picking lleva salida_acopio_id → manifiesto_salida_id.

Nota crítica: en ENTRADA el lot_id del stock.move.line NO suele coincidir con
el lot_id de manifiesto.ambiental.residuo porque el producto del manifiesto
difiere del producto consumible elegido en la recepción. Ambos lotes tienen
el mismo `name` (número de manifiesto) pero son registros distintos. Por eso
el matching del residuo_line se hace con fallbacks robustos.
"""
from odoo import models, fields, api
import logging

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _find_residuo_line_in_manifiesto(env, manifiesto, lot):
    """
    Dado un manifiesto y un lote, encuentra la línea de residuo más probable.

    Estrategia (en orden):
      1. Match directo por lot_id.
      2. Si el manifiesto tiene una sola línea, usarla.
      3. Match por product_id del lote.
      4. Match por nombre del producto del lote (ilike) sobre nombre_residuo.
    """
    if not manifiesto:
        return None

    Residuo = env['manifiesto.ambiental.residuo']

    if lot:
        rl = Residuo.search([
            ('manifiesto_id', '=', manifiesto.id),
            ('lot_id', '=', lot.id),
        ], limit=1)
        if rl:
            return rl

    if len(manifiesto.residuo_ids) == 1:
        return manifiesto.residuo_ids[0]

    if lot and lot.product_id:
        rl = Residuo.search([
            ('manifiesto_id', '=', manifiesto.id),
            ('product_id', '=', lot.product_id.id),
        ], limit=1)
        if rl:
            return rl

        product_name = (lot.product_id.name or '').strip()
        if product_name:
            rl = Residuo.search([
                ('manifiesto_id', '=', manifiesto.id),
                ('nombre_residuo', 'ilike', product_name),
            ], limit=1)
            if rl:
                return rl

    return None


def _get_manifiesto_and_residuo(env, lot, manifiesto_override=None):
    """
    Resuelve (manifiesto, residuo_line) para un lot.

    - Si manifiesto_override se provee (salida via salida_acopio_id, o entrada
      via residuo.recepcion desde picking.origin), ese manifiesto tiene
      prioridad y el residuo_line se busca dentro de él con matching robusto.
    - Si no, se busca por lot_id directo, preferiendo entrada vigente, y
      finalmente fallback por numero_manifiesto = lot.name.
    """
    if not lot and not manifiesto_override:
        return None, None

    if manifiesto_override:
        return manifiesto_override, _find_residuo_line_in_manifiesto(env, manifiesto_override, lot)

    Residuo = env['manifiesto.ambiental.residuo']
    Manifiesto = env['manifiesto.ambiental']

    # Match directo por lot_id (prefiriendo entradas vigentes)
    residuo_line = Residuo.search([
        ('lot_id', '=', lot.id),
        ('manifiesto_id.tipo_manifiesto', '=', 'entrada'),
        ('manifiesto_id.is_current_version', '=', True),
    ], limit=1, order='id desc')
    if not residuo_line:
        residuo_line = Residuo.search(
            [('lot_id', '=', lot.id)], limit=1, order='id desc'
        )
    if residuo_line and residuo_line.manifiesto_id:
        return residuo_line.manifiesto_id, residuo_line

    # Fallback: buscar manifiesto por numero_manifiesto == lot.name
    manifiesto = Manifiesto.search([
        ('numero_manifiesto', '=', lot.name),
        ('is_current_version', '=', True),
        ('tipo_manifiesto', '=', 'entrada'),
    ], limit=1)
    if not manifiesto:
        manifiesto = Manifiesto.search([
            ('numero_manifiesto', '=', lot.name),
            ('is_current_version', '=', True),
        ], limit=1)

    if manifiesto:
        return manifiesto, _find_residuo_line_in_manifiesto(env, manifiesto, lot)

    return None, None


def _get_salida_manifiesto(picking):
    """Soft check: picking.salida_acopio_id → manifiesto_salida_id."""
    if not picking:
        return None
    if 'salida_acopio_id' not in picking._fields:
        return None
    salida = picking.salida_acopio_id
    if not salida:
        return None
    return salida.manifiesto_salida_id or None


def _get_recepcion_manifiesto(picking):
    """
    Para manifiestos de ENTRADA: residuo.recepcion crea el picking y escribe
    su `name` en `picking.origin`. Desde la recepción llegamos al manifiesto.
    """
    if not picking or not picking.origin:
        return None
    Rec = picking.env['residuo.recepcion']
    if 'manifiesto_id' not in Rec._fields:
        return None
    recepcion = Rec.search([('name', '=', picking.origin)], limit=1)
    if not recepcion:
        return None
    return recepcion.manifiesto_id or None


def _falsy_values():
    return {
        'folio': 0,
        'numero_manifiesto': False,
        'fecha_manifiesto': False,
        'generador_nombre': False,
        'numero_registro_ambiental': False,
        'nombre_operador': False,
        'camion': False,
        'camion_contenedor_placa': False,
        'transportista_nombre': False,
        'autorizacion_transportista': False,
        'nombre_residuo': False,
        'cantidad_contenedores': 0,
        'tipo_contenedor': False,
        'capacidad_contenedor': False,
        'clasificaciones_cretib': False,
        'clasificacion_c': False,
        'clasificacion_r': False,
        'clasificacion_e': False,
        'clasificacion_t': False,
        'clasificacion_i': False,
        'clasificacion_b': False,
        'plan_manejo': False,
        'tipo_manejo_id_rel': False,
        'fecha_recepcion_residuo': False,
        'fecha_caducidad_residuo': False,
        'dias_restantes_caducidad': 0,
        'caducidad_estado': False,
    }


def _fill_from_manifiesto_and_lot(record, manifiesto, residuo_line, lot):
    """Escribe en record todos los campos computados."""
    vals = _falsy_values()

    if manifiesto:
        vals['folio'] = manifiesto.sequence_number or 0
        vals['numero_manifiesto'] = manifiesto.numero_manifiesto or False
        vals['fecha_manifiesto'] = manifiesto.generador_fecha or False
        vals['generador_nombre'] = manifiesto.generador_nombre or False
        vals['numero_registro_ambiental'] = manifiesto.numero_registro_ambiental or False
        vals['nombre_operador'] = manifiesto.transportista_responsable_nombre or False
        vals['camion'] = manifiesto.tipo_vehiculo or False
        vals['camion_contenedor_placa'] = manifiesto.numero_placa or False
        vals['transportista_nombre'] = manifiesto.transportista_nombre or False
        vals['autorizacion_transportista'] = manifiesto.numero_autorizacion_semarnat or False

    if residuo_line:
        vals['nombre_residuo'] = residuo_line.nombre_residuo or False

        if residuo_line.packaging_id:
            vals['tipo_contenedor'] = residuo_line.packaging_id.name
        elif residuo_line.envase_tipo:
            tipo_sel = dict(residuo_line._fields['envase_tipo'].selection)
            vals['tipo_contenedor'] = tipo_sel.get(residuo_line.envase_tipo, residuo_line.envase_tipo)
        vals['capacidad_contenedor'] = residuo_line.envase_capacidad or False
        vals['cantidad_contenedores'] = residuo_line.envase_cantidad or 0

        vals['clasificaciones_cretib'] = residuo_line.clasificaciones_display or False
        vals['clasificacion_c'] = residuo_line.clasificacion_corrosivo
        vals['clasificacion_r'] = residuo_line.clasificacion_reactivo
        vals['clasificacion_e'] = residuo_line.clasificacion_explosivo
        vals['clasificacion_t'] = residuo_line.clasificacion_toxico
        vals['clasificacion_i'] = residuo_line.clasificacion_inflamable
        vals['clasificacion_b'] = residuo_line.clasificacion_biologico

    if lot:
        if not residuo_line:
            vals['clasificaciones_cretib'] = lot.clasificaciones_display or False
            vals['clasificacion_c'] = lot.clasificacion_corrosivo
            vals['clasificacion_r'] = lot.clasificacion_reactivo
            vals['clasificacion_e'] = lot.clasificacion_explosivo
            vals['clasificacion_t'] = lot.clasificacion_toxico
            vals['clasificacion_i'] = lot.clasificacion_inflamable
            vals['clasificacion_b'] = lot.clasificacion_biologico

        vals['tipo_manejo_id_rel'] = lot.tipo_manejo_id.id if lot.tipo_manejo_id else False
        vals['plan_manejo'] = lot.tipo_manejo_id.name if lot.tipo_manejo_id else False
        vals['fecha_recepcion_residuo'] = lot.fecha_recepcion_residuo or False
        vals['fecha_caducidad_residuo'] = lot.fecha_caducidad_residuo or False
        vals['dias_restantes_caducidad'] = lot.dias_restantes_caducidad or 0
        vals['caducidad_estado'] = lot.caducidad_estado or False

    for k, v in vals.items():
        setattr(record, k, v)


# ---------------------------------------------------------------------------
# stock.quant — Reporte de Ubicaciones
# ---------------------------------------------------------------------------
class StockQuantResiduo(models.Model):
    _inherit = 'stock.quant'

    folio = fields.Integer(string='Folio', compute='_compute_sai_fields', store=False)
    numero_manifiesto = fields.Char(string='No. Manifiesto', compute='_compute_sai_fields', store=False)
    fecha_manifiesto = fields.Date(string='Fecha', compute='_compute_sai_fields', store=False)
    generador_nombre = fields.Char(string='Generador', compute='_compute_sai_fields', store=False)
    numero_registro_ambiental = fields.Char(string='Núm. Reg. Ambiental', compute='_compute_sai_fields', store=False)
    nombre_operador = fields.Char(string='Nombre del Operador', compute='_compute_sai_fields', store=False)
    camion = fields.Char(string='Camión', compute='_compute_sai_fields', store=False)
    camion_contenedor_placa = fields.Char(string='Camión-Contenedor-Placa', compute='_compute_sai_fields', store=False)
    transportista_nombre = fields.Char(string='Transportista', compute='_compute_sai_fields', store=False)
    autorizacion_transportista = fields.Char(string='Autorización Transportista', compute='_compute_sai_fields', store=False)

    nombre_residuo = fields.Char(string='Residuos Peligrosos', compute='_compute_sai_fields', store=False)
    cantidad_contenedores = fields.Integer(string='Cant. Contenedores', compute='_compute_sai_fields', store=False)
    tipo_contenedor = fields.Char(string='Tipo Contenedor', compute='_compute_sai_fields', store=False)
    capacidad_contenedor = fields.Char(string='Capacidad Contenedor', compute='_compute_sai_fields', store=False)
    clasificaciones_cretib = fields.Char(string='CRETIB', compute='_compute_sai_fields', store=False)
    clasificacion_c = fields.Boolean(string='C', compute='_compute_sai_fields', store=False)
    clasificacion_r = fields.Boolean(string='R', compute='_compute_sai_fields', store=False)
    clasificacion_e = fields.Boolean(string='E', compute='_compute_sai_fields', store=False)
    clasificacion_t = fields.Boolean(string='T', compute='_compute_sai_fields', store=False)
    clasificacion_i = fields.Boolean(string='I', compute='_compute_sai_fields', store=False)
    clasificacion_b = fields.Boolean(string='B', compute='_compute_sai_fields', store=False)

    plan_manejo = fields.Char(string='Plan de Manejo', compute='_compute_sai_fields', store=False)
    tipo_manejo_id_rel = fields.Many2one(
        'residuo.tipo.manejo', string='Tipo de Manejo',
        compute='_compute_sai_fields', store=False
    )
    fecha_recepcion_residuo = fields.Date(string='Fecha Recepción', compute='_compute_sai_fields', store=False)
    fecha_caducidad_residuo = fields.Date(string='Fecha Caducidad', compute='_compute_sai_fields', store=False)
    dias_restantes_caducidad = fields.Integer(string='Días Restantes', compute='_compute_sai_fields', store=False)
    caducidad_estado = fields.Selection(
        [('ok', 'Vigente'), ('warning', 'Próximo a vencer'), ('expired', 'Vencido')],
        string='Estado Caducidad', compute='_compute_sai_fields', store=False
    )

    @api.depends('lot_id')
    def _compute_sai_fields(self):
        for quant in self:
            manifiesto, residuo_line = _get_manifiesto_and_residuo(self.env, quant.lot_id)
            _fill_from_manifiesto_and_lot(quant, manifiesto, residuo_line, quant.lot_id)


# ---------------------------------------------------------------------------
# stock.move.line — Historial de Movimientos
# ---------------------------------------------------------------------------
class StockMoveLineResiduo(models.Model):
    _inherit = 'stock.move.line'

    folio = fields.Integer(string='Folio', compute='_compute_sai_fields', store=False)
    numero_manifiesto = fields.Char(string='No. Manifiesto', compute='_compute_sai_fields', store=False)
    fecha_manifiesto = fields.Date(string='Fecha', compute='_compute_sai_fields', store=False)
    generador_nombre = fields.Char(string='Generador', compute='_compute_sai_fields', store=False)
    numero_registro_ambiental = fields.Char(string='Núm. Reg. Ambiental', compute='_compute_sai_fields', store=False)
    nombre_operador = fields.Char(string='Nombre del Operador', compute='_compute_sai_fields', store=False)
    camion = fields.Char(string='Camión', compute='_compute_sai_fields', store=False)
    camion_contenedor_placa = fields.Char(string='Camión-Contenedor-Placa', compute='_compute_sai_fields', store=False)
    transportista_nombre = fields.Char(string='Transportista', compute='_compute_sai_fields', store=False)
    autorizacion_transportista = fields.Char(string='Autorización Transportista', compute='_compute_sai_fields', store=False)

    nombre_residuo = fields.Char(string='Residuos Peligrosos', compute='_compute_sai_fields', store=False)
    cantidad_contenedores = fields.Integer(string='Cant. Contenedores', compute='_compute_sai_fields', store=False)
    tipo_contenedor = fields.Char(string='Tipo Contenedor', compute='_compute_sai_fields', store=False)
    capacidad_contenedor = fields.Char(string='Capacidad Contenedor', compute='_compute_sai_fields', store=False)
    clasificaciones_cretib = fields.Char(string='CRETIB', compute='_compute_sai_fields', store=False)
    clasificacion_c = fields.Boolean(string='C', compute='_compute_sai_fields', store=False)
    clasificacion_r = fields.Boolean(string='R', compute='_compute_sai_fields', store=False)
    clasificacion_e = fields.Boolean(string='E', compute='_compute_sai_fields', store=False)
    clasificacion_t = fields.Boolean(string='T', compute='_compute_sai_fields', store=False)
    clasificacion_i = fields.Boolean(string='I', compute='_compute_sai_fields', store=False)
    clasificacion_b = fields.Boolean(string='B', compute='_compute_sai_fields', store=False)

    plan_manejo = fields.Char(string='Plan de Manejo', compute='_compute_sai_fields', store=False)
    tipo_manejo_id_rel = fields.Many2one(
        'residuo.tipo.manejo', string='Tipo de Manejo',
        compute='_compute_sai_fields', store=False
    )
    fecha_recepcion_residuo = fields.Date(string='Fecha Recepción', compute='_compute_sai_fields', store=False)
    fecha_caducidad_residuo = fields.Date(string='Fecha Caducidad', compute='_compute_sai_fields', store=False)
    dias_restantes_caducidad = fields.Integer(string='Días Restantes', compute='_compute_sai_fields', store=False)
    caducidad_estado = fields.Selection(
        [('ok', 'Vigente'), ('warning', 'Próximo a vencer'), ('expired', 'Vencido')],
        string='Estado Caducidad', compute='_compute_sai_fields', store=False
    )

    @api.depends('lot_id', 'picking_id')
    def _compute_sai_fields(self):
        for line in self:
            # Prioridad 1: salida (picking.salida_acopio_id)
            manifiesto_override = _get_salida_manifiesto(line.picking_id)
            # Prioridad 2: entrada via recepción (picking.origin → residuo.recepcion)
            if not manifiesto_override:
                manifiesto_override = _get_recepcion_manifiesto(line.picking_id)

            manifiesto, residuo_line = _get_manifiesto_and_residuo(
                self.env, line.lot_id, manifiesto_override=manifiesto_override
            )
            _fill_from_manifiesto_and_lot(line, manifiesto, residuo_line, line.lot_id)