# -*- coding: utf-8 -*-
"""
Extensión de stock.quant y stock.move.line para exponer todos los campos
del registro de manifiestos de residuos peligrosos SAI.

Cadena de matching residuo → reporte (en orden de fiabilidad):
  0. Vínculo directo residuo.recepcion.linea.residuo_manifiesto_id
  1. descripcion_origen de la línea de recepción == nombre_residuo
  2. lot_id idéntico
  3. product_id del lote == product_id del residuo
  4. Nombre limpio (sin [CODE]) del producto == nombre_residuo limpio
  5. descripcion_origen limpia == nombre_residuo limpio
  6. Manifiesto con un solo residuo → esa única línea
"""
import re
import logging
from odoo import models, fields, api

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Normalización de nombres
# ---------------------------------------------------------------------------
_CODE_PREFIX_RE = re.compile(r'^\s*\[[^\]]+\]\s*')


def _clean_name(name):
    """Quita prefijos [CODE] y normaliza para comparación exacta."""
    if not name:
        return ''
    cleaned = _CODE_PREFIX_RE.sub('', name)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip().upper()
    return cleaned


# ---------------------------------------------------------------------------
# Navegación picking → recepción → manifiesto
# ---------------------------------------------------------------------------
def _get_recepcion_from_picking(picking):
    if not picking or not picking.origin:
        return None
    Rec = picking.env['residuo.recepcion']
    return Rec.search([('name', '=', picking.origin)], limit=1) or None


def _get_recepcion_manifiesto(picking):
    recepcion = _get_recepcion_from_picking(picking)
    if not recepcion or 'manifiesto_id' not in recepcion._fields:
        return None
    return recepcion.manifiesto_id or None


def _get_recepcion_linea_for_move(move_line):
    """Encuentra la línea de recepción que corresponde a un stock.move.line."""
    if not move_line or not move_line.product_id:
        return None
    recepcion = _get_recepcion_from_picking(move_line.picking_id)
    if not recepcion:
        return None

    product_id = move_line.product_id.id

    # Preferir mismo producto + mismo lote
    if move_line.lot_id:
        lot_name = move_line.lot_id.name
        match = recepcion.linea_ids.filtered(
            lambda l: l.product_id.id == product_id
            and (l.lote_asignado or '') == (lot_name or '')
        )
        if match:
            return match[0]

    # Fallback: sólo producto
    match = recepcion.linea_ids.filtered(lambda l: l.product_id.id == product_id)
    if match:
        return match[0]

    # Último recurso: mismo lote
    if move_line.lot_id:
        lot_name = move_line.lot_id.name
        match = recepcion.linea_ids.filtered(
            lambda l: (l.lote_asignado or '') == (lot_name or '')
        )
        if match:
            return match[0]

    return None


def _get_salida_manifiesto(picking):
    if not picking or 'salida_acopio_id' not in picking._fields:
        return None
    salida = picking.salida_acopio_id
    if not salida:
        return None
    return salida.manifiesto_salida_id or None


# ---------------------------------------------------------------------------
# Match residuo_line dentro del manifiesto
# ---------------------------------------------------------------------------
def _find_residuo_line_in_manifiesto(env, manifiesto, lot, recepcion_linea=None,
                                     move_line=None):
    if not manifiesto:
        return None

    Residuo = env['manifiesto.ambiental.residuo']

    # 0. Vínculo explícito (nuevo campo)
    if recepcion_linea and 'residuo_manifiesto_id' in recepcion_linea._fields:
        rm = recepcion_linea.residuo_manifiesto_id
        if rm and rm.manifiesto_id.id == manifiesto.id:
            return rm

    # 1. descripcion_origen == nombre_residuo (match exacto)
    if recepcion_linea and recepcion_linea.descripcion_origen:
        desc = recepcion_linea.descripcion_origen.strip()
        rl = Residuo.search([
            ('manifiesto_id', '=', manifiesto.id),
            ('nombre_residuo', '=', desc),
        ], limit=1)
        if rl:
            return rl

    # 2. lot_id idéntico
    if lot:
        rl = Residuo.search([
            ('manifiesto_id', '=', manifiesto.id),
            ('lot_id', '=', lot.id),
        ], limit=1)
        if rl:
            return rl

    # 3. product_id del lote == product_id del residuo
    if lot and lot.product_id:
        rl = Residuo.search([
            ('manifiesto_id', '=', manifiesto.id),
            ('product_id', '=', lot.product_id.id),
        ], limit=1)
        if rl:
            return rl

    # También probar con el product_id del move_line (por si el lote está limpio)
    if move_line and move_line.product_id:
        rl = Residuo.search([
            ('manifiesto_id', '=', manifiesto.id),
            ('product_id', '=', move_line.product_id.id),
        ], limit=1)
        if rl:
            return rl

    # 4. Nombre limpio del producto == nombre_residuo limpio
    candidate_product_name = ''
    if lot and lot.product_id:
        candidate_product_name = lot.product_id.name
    elif move_line and move_line.product_id:
        candidate_product_name = move_line.product_id.name

    clean_product = _clean_name(candidate_product_name)
    if clean_product:
        for residuo in manifiesto.residuo_ids:
            if _clean_name(residuo.nombre_residuo) == clean_product:
                return residuo

    # 5. descripcion_origen limpia == nombre_residuo limpio
    if recepcion_linea and recepcion_linea.descripcion_origen:
        clean_desc = _clean_name(recepcion_linea.descripcion_origen)
        if clean_desc:
            for residuo in manifiesto.residuo_ids:
                if _clean_name(residuo.nombre_residuo) == clean_desc:
                    return residuo

    # 6. Única línea
    if len(manifiesto.residuo_ids) == 1:
        return manifiesto.residuo_ids[0]

    return None


def _get_manifiesto_and_residuo(env, lot, move_line=None,
                                manifiesto_override=None, recepcion_linea=None):
    if move_line and recepcion_linea is None:
        recepcion_linea = _get_recepcion_linea_for_move(move_line)

    if manifiesto_override:
        return manifiesto_override, _find_residuo_line_in_manifiesto(
            env, manifiesto_override, lot, recepcion_linea, move_line
        )

    if not lot:
        return None, None

    Residuo = env['manifiesto.ambiental.residuo']
    Manifiesto = env['manifiesto.ambiental']

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
        return manifiesto, _find_residuo_line_in_manifiesto(
            env, manifiesto, lot, recepcion_linea, move_line
        )

    return None, None


# ---------------------------------------------------------------------------
# Llenado de campos
# ---------------------------------------------------------------------------
def _falsy_values():
    return {
        'folio': 0, 'numero_manifiesto': False, 'fecha_manifiesto': False,
        'generador_nombre': False, 'generador_origen': False,
        'numero_registro_ambiental': False,
        'nombre_operador': False, 'camion': False,
        'camion_contenedor_placa': False, 'transportista_nombre': False,
        'autorizacion_transportista': False,
        'nombre_residuo': False, 'cantidad_contenedores': 0,
        'tipo_contenedor': False, 'capacidad_contenedor': False,
        'clasificaciones_cretib': False,
        'clasificacion_c': False, 'clasificacion_r': False,
        'clasificacion_e': False, 'clasificacion_t': False,
        'clasificacion_i': False, 'clasificacion_b': False,
        'plan_manejo': False, 'tipo_manejo_id_rel': False,
        'fecha_recepcion_residuo': False, 'fecha_caducidad_residuo': False,
        'dias_restantes_caducidad': 0, 'caducidad_estado': False,
    }


def _fill_from_sources(record, manifiesto, residuo_line, lot,
                       recepcion=None, recepcion_linea=None,
                       entrada_manifiesto=None):
    vals = _falsy_values()

    if manifiesto:
        vals['folio'] = manifiesto.sequence_number or 0
        vals['numero_manifiesto'] = manifiesto.numero_manifiesto or False
        vals['fecha_manifiesto'] = manifiesto.generador_fecha or False
        vals['generador_nombre'] = manifiesto.generador_nombre or False

    # Generador de origen: siempre del manifiesto de ENTRADA del lote, para poder
    # trazar las salidas por el cliente generador original (el manifiesto de salida
    # trae como generador al acopio, no al cliente).
    if entrada_manifiesto:
        vals['generador_origen'] = entrada_manifiesto.generador_nombre or False
    elif manifiesto and manifiesto.tipo_manifiesto == 'entrada':
        vals['generador_origen'] = manifiesto.generador_nombre or False
    if manifiesto:
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
            vals['tipo_contenedor'] = tipo_sel.get(
                residuo_line.envase_tipo, residuo_line.envase_tipo
            )
        vals['capacidad_contenedor'] = residuo_line.envase_capacidad or False
        vals['cantidad_contenedores'] = residuo_line.envase_cantidad or 0
        vals['clasificaciones_cretib'] = residuo_line.clasificaciones_display or False
        vals['clasificacion_c'] = residuo_line.clasificacion_corrosivo
        vals['clasificacion_r'] = residuo_line.clasificacion_reactivo
        vals['clasificacion_e'] = residuo_line.clasificacion_explosivo
        vals['clasificacion_t'] = residuo_line.clasificacion_toxico
        vals['clasificacion_i'] = residuo_line.clasificacion_inflamable
        vals['clasificacion_b'] = residuo_line.clasificacion_biologico

    # Fallback desde recepcion_linea
    if recepcion_linea:
        if not vals['nombre_residuo'] and recepcion_linea.descripcion_origen:
            vals['nombre_residuo'] = recepcion_linea.descripcion_origen
        if not vals['clasificaciones_cretib']:
            vals['clasificaciones_cretib'] = recepcion_linea.clasificaciones_display or False
            vals['clasificacion_c'] = recepcion_linea.clasificacion_corrosivo
            vals['clasificacion_r'] = recepcion_linea.clasificacion_reactivo
            vals['clasificacion_e'] = recepcion_linea.clasificacion_explosivo
            vals['clasificacion_t'] = recepcion_linea.clasificacion_toxico
            vals['clasificacion_i'] = recepcion_linea.clasificacion_inflamable
            vals['clasificacion_b'] = recepcion_linea.clasificacion_biologico
        if recepcion_linea.tipo_manejo_id:
            vals['tipo_manejo_id_rel'] = recepcion_linea.tipo_manejo_id.id
            vals['plan_manejo'] = recepcion_linea.tipo_manejo_id.name

    # Lote (caducidad + backup de tipo_manejo y CRETIB)
    if lot:
        if not vals['clasificaciones_cretib']:
            vals['clasificaciones_cretib'] = lot.clasificaciones_display or False
            vals['clasificacion_c'] = lot.clasificacion_corrosivo
            vals['clasificacion_r'] = lot.clasificacion_reactivo
            vals['clasificacion_e'] = lot.clasificacion_explosivo
            vals['clasificacion_t'] = lot.clasificacion_toxico
            vals['clasificacion_i'] = lot.clasificacion_inflamable
            vals['clasificacion_b'] = lot.clasificacion_biologico

        if not vals['tipo_manejo_id_rel'] and lot.tipo_manejo_id:
            vals['tipo_manejo_id_rel'] = lot.tipo_manejo_id.id
            vals['plan_manejo'] = lot.tipo_manejo_id.name

        vals['fecha_recepcion_residuo'] = lot.fecha_recepcion_residuo or False
        vals['fecha_caducidad_residuo'] = lot.fecha_caducidad_residuo or False
        vals['dias_restantes_caducidad'] = lot.dias_restantes_caducidad or 0
        vals['caducidad_estado'] = lot.caducidad_estado or False

    if recepcion and not vals['fecha_recepcion_residuo']:
        if recepcion.fecha_recepcion:
            vals['fecha_recepcion_residuo'] = recepcion.fecha_recepcion

    for k, v in vals.items():
        setattr(record, k, v)


# ---------------------------------------------------------------------------
# stock.quant
# ---------------------------------------------------------------------------
class StockQuantResiduo(models.Model):
    _inherit = 'stock.quant'

    folio = fields.Integer(string='Folio', compute='_compute_sai_fields', store=True)
    numero_manifiesto = fields.Char(string='No. Manifiesto', compute='_compute_sai_fields', store=True)
    fecha_manifiesto = fields.Date(string='Fecha', compute='_compute_sai_fields', store=True)
    generador_nombre = fields.Char(string='Generador', compute='_compute_sai_fields', store=True)
    generador_origen = fields.Char(string='Generador de Origen', compute='_compute_sai_fields', store=True)
    numero_registro_ambiental = fields.Char(string='Núm. Reg. Ambiental', compute='_compute_sai_fields', store=True)
    nombre_operador = fields.Char(string='Nombre del Operador', compute='_compute_sai_fields', store=True)
    camion = fields.Char(string='Camión', compute='_compute_sai_fields', store=True)
    camion_contenedor_placa = fields.Char(string='Camión-Contenedor-Placa', compute='_compute_sai_fields', store=True)
    transportista_nombre = fields.Char(string='Transportista', compute='_compute_sai_fields', store=True)
    autorizacion_transportista = fields.Char(string='Autorización Transportista', compute='_compute_sai_fields', store=True)

    nombre_residuo = fields.Char(string='Residuos Peligrosos', compute='_compute_sai_fields', store=True)
    cantidad_contenedores = fields.Integer(string='Cant. Contenedores', compute='_compute_sai_fields', store=True)
    tipo_contenedor = fields.Char(string='Tipo Contenedor', compute='_compute_sai_fields', store=True)
    capacidad_contenedor = fields.Char(string='Capacidad Contenedor', compute='_compute_sai_fields', store=True)
    clasificaciones_cretib = fields.Char(string='CRETIB', compute='_compute_sai_fields', store=True)
    clasificacion_c = fields.Boolean(string='C', compute='_compute_sai_fields', store=True)
    clasificacion_r = fields.Boolean(string='R', compute='_compute_sai_fields', store=True)
    clasificacion_e = fields.Boolean(string='E', compute='_compute_sai_fields', store=True)
    clasificacion_t = fields.Boolean(string='T', compute='_compute_sai_fields', store=True)
    clasificacion_i = fields.Boolean(string='I', compute='_compute_sai_fields', store=True)
    clasificacion_b = fields.Boolean(string='B', compute='_compute_sai_fields', store=True)

    plan_manejo = fields.Char(string='Plan de Manejo', compute='_compute_sai_fields', store=True)
    tipo_manejo_id_rel = fields.Many2one(
        'residuo.tipo.manejo', string='Tipo de Manejo',
        compute='_compute_sai_fields', store=True,
    )
    fecha_recepcion_residuo = fields.Date(string='Fecha Recepción', compute='_compute_sai_fields', store=True)
    fecha_caducidad_residuo = fields.Date(string='Fecha Caducidad', compute='_compute_sai_fields', store=True)
    dias_restantes_caducidad = fields.Integer(string='Días Restantes', compute='_compute_sai_fields', store=True)
    caducidad_estado = fields.Selection(
        [('ok', 'Vigente'), ('warning', 'Próximo a vencer'), ('expired', 'Vencido')],
        string='Estado Caducidad', compute='_compute_sai_fields', store=True,
    )

    @api.depends('lot_id')
    def _compute_sai_fields(self):
        for quant in self:
            manifiesto, residuo_line = _get_manifiesto_and_residuo(self.env, quant.lot_id)
            _fill_from_sources(quant, manifiesto, residuo_line, quant.lot_id,
                               entrada_manifiesto=manifiesto)


# ---------------------------------------------------------------------------
# stock.move.line
# ---------------------------------------------------------------------------
class StockMoveLineResiduo(models.Model):
    _inherit = 'stock.move.line'

    folio = fields.Integer(string='Folio', compute='_compute_sai_fields', store=True)
    numero_manifiesto = fields.Char(string='No. Manifiesto', compute='_compute_sai_fields', store=True)
    fecha_manifiesto = fields.Date(string='Fecha', compute='_compute_sai_fields', store=True)
    generador_nombre = fields.Char(string='Generador', compute='_compute_sai_fields', store=True)
    generador_origen = fields.Char(string='Generador de Origen', compute='_compute_sai_fields', store=True)
    numero_registro_ambiental = fields.Char(string='Núm. Reg. Ambiental', compute='_compute_sai_fields', store=True)
    nombre_operador = fields.Char(string='Nombre del Operador', compute='_compute_sai_fields', store=True)
    camion = fields.Char(string='Camión', compute='_compute_sai_fields', store=True)
    camion_contenedor_placa = fields.Char(string='Camión-Contenedor-Placa', compute='_compute_sai_fields', store=True)
    transportista_nombre = fields.Char(string='Transportista', compute='_compute_sai_fields', store=True)
    autorizacion_transportista = fields.Char(string='Autorización Transportista', compute='_compute_sai_fields', store=True)

    nombre_residuo = fields.Char(string='Residuos Peligrosos', compute='_compute_sai_fields', store=True)
    cantidad_contenedores = fields.Integer(string='Cant. Contenedores', compute='_compute_sai_fields', store=True)
    tipo_contenedor = fields.Char(string='Tipo Contenedor', compute='_compute_sai_fields', store=True)
    capacidad_contenedor = fields.Char(string='Capacidad Contenedor', compute='_compute_sai_fields', store=True)
    clasificaciones_cretib = fields.Char(string='CRETIB', compute='_compute_sai_fields', store=True)
    clasificacion_c = fields.Boolean(string='C', compute='_compute_sai_fields', store=True)
    clasificacion_r = fields.Boolean(string='R', compute='_compute_sai_fields', store=True)
    clasificacion_e = fields.Boolean(string='E', compute='_compute_sai_fields', store=True)
    clasificacion_t = fields.Boolean(string='T', compute='_compute_sai_fields', store=True)
    clasificacion_i = fields.Boolean(string='I', compute='_compute_sai_fields', store=True)
    clasificacion_b = fields.Boolean(string='B', compute='_compute_sai_fields', store=True)

    plan_manejo = fields.Char(string='Plan de Manejo', compute='_compute_sai_fields', store=True)
    tipo_manejo_id_rel = fields.Many2one(
        'residuo.tipo.manejo', string='Tipo de Manejo',
        compute='_compute_sai_fields', store=True,
    )
    fecha_recepcion_residuo = fields.Date(string='Fecha Recepción', compute='_compute_sai_fields', store=True)
    fecha_caducidad_residuo = fields.Date(string='Fecha Caducidad', compute='_compute_sai_fields', store=True)
    dias_restantes_caducidad = fields.Integer(string='Días Restantes', compute='_compute_sai_fields', store=True)
    caducidad_estado = fields.Selection(
        [('ok', 'Vigente'), ('warning', 'Próximo a vencer'), ('expired', 'Vencido')],
        string='Estado Caducidad', compute='_compute_sai_fields', store=True,
    )

    @api.depends('lot_id', 'picking_id', 'product_id')
    def _compute_sai_fields(self):
        for line in self:
            manifiesto_override = _get_salida_manifiesto(line.picking_id)
            if not manifiesto_override:
                manifiesto_override = _get_recepcion_manifiesto(line.picking_id)

            recepcion = _get_recepcion_from_picking(line.picking_id)
            recepcion_linea = _get_recepcion_linea_for_move(line)

            manifiesto, residuo_line = _get_manifiesto_and_residuo(
                self.env, line.lot_id,
                move_line=line,
                manifiesto_override=manifiesto_override,
                recepcion_linea=recepcion_linea,
            )

            # Manifiesto de ENTRADA del lote (sin override de salida), para trazar
            # el generador de origen aunque este movimiento sea una salida. Si el
            # manifiesto resuelto ya es de entrada, _fill_from_sources lo deduce solo.
            entrada_manifiesto = None
            if manifiesto and manifiesto.tipo_manifiesto != 'entrada':
                entrada_manifiesto, _ = _get_manifiesto_and_residuo(
                    self.env, line.lot_id,
                    move_line=line,
                    recepcion_linea=recepcion_linea,
                )

            _fill_from_sources(
                line, manifiesto, residuo_line, line.lot_id,
                recepcion=recepcion, recepcion_linea=recepcion_linea,
                entrada_manifiesto=entrada_manifiesto,
            )