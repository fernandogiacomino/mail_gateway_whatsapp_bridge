from odoo import api, models, fields
from odoo.exceptions import ValidationError



class MailGateway(models.Model):
    """
    Extensión de mail.gateway para WhatsApp Bridge.
    
    Agrega campos para:
    - route_to_livechat: Habilitar ruteo a Live Chat
    - livechat_channel_id: Canal de Live Chat destino
    - whatsapp_phone_prefix: Prefijo de país ajustable para búsqueda de partner
    
    La lógica de interceptación de canales está en
    mail_gateway_whatsapp_service._get_channel()
    """
    _inherit = "mail.gateway"

    route_to_livechat = fields.Boolean(
        string="Rutear a Live Chat",
        default=False,
           help="Si está marcado, los mensajes entrantes de WhatsApp se "
               "desvían a un canal de Live Chat en lugar de crear un canal "
               "gateway. Útil para atender con bots o agentes de soporte."
    )
    
    livechat_channel_id = fields.Many2one(
        "im_livechat.channel",
        string="Canal Live Chat destino",
           help="Canal de Live Chat donde se desviarán los mensajes de WhatsApp. "
               "Los contactos aparecerán como guests en este canal."
    )

    whatsapp_phone_prefix = fields.Char(
           string="Prefijo de país a ajustar (WhatsApp)",
           help="Prefijo que WhatsApp añade en ciertos países tras el código de país. "
               "Ejemplo: En Argentina, WhatsApp añade '9' después del +54. "
               "Deja vacío si no necesitas ajustar. "
               "Se usará al buscar partners por teléfono para intentar coincidencias "
             "con y sin este prefijo."
    )

    whatsapp_auto_close_after_minutes = fields.Integer(
        string="Cerrar conversación tras inactividad (min)",
        default=0,
        help=(
            "Si es mayor a 0, las conversaciones WhatsApp-Livechat se marcarán "
            "como cerradas automáticamente cuando no tengan actividad en ese período."
        ),
    )

    whatsapp_auto_delete_closed_after_days = fields.Integer(
        string="Eliminar conversaciones cerradas tras (días)",
        default=0,
        help=(
            "Si es mayor a 0, las conversaciones cerradas se eliminarán "
            "automáticamente al superar esta antigüedad."
        ),
    )

    @api.constrains("route_to_livechat", "gateway_type", "whatsapp_version", "token")
    def _check_whatsapp_livechat_runtime_config(self):
        for gateway in self:
            if gateway.gateway_type != "whatsapp" or not gateway.route_to_livechat:
                continue
            if not gateway.whatsapp_version:
                raise ValidationError(
                    self.env._(
                        "La versión de WhatsApp API es obligatoria para rutear a Live Chat."
                    )
                )
            if not gateway.token:
                raise ValidationError(
                    self.env._(
                        "El token de WhatsApp es obligatorio para rutear a Live Chat."
                    )
                )
            if gateway.whatsapp_auto_close_after_minutes < 0:
                raise ValidationError(
                    self.env._(
                        "El cierre automático por inactividad debe ser 0 o mayor."
                    )
                )
            if gateway.whatsapp_auto_delete_closed_after_days < 0:
                raise ValidationError(
                    self.env._(
                        "La eliminación automática de conversaciones cerradas debe ser 0 o mayor."
                    )
                )
