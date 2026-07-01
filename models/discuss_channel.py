import re
import hashlib
import logging
from datetime import timedelta
from odoo import api, models, fields
from odoo.exceptions import ValidationError
from odoo.tools import html2plaintext, plaintext2html
import requests


_logger = logging.getLogger(__name__)



class DiscussChannel(models.Model):
    """
    ExtensiÃ³n de discuss.channel para WhatsApp Bridge.
    
    Campos:
    - whatsapp_gateway_id: referencia al gateway de WhatsApp
    
    Funcionalidades:
    - Intercepta message_post() para detectar respuestas en Live Chat
    - EnvÃ­a respuestas de vuelta a WhatsApp
    """
    _inherit = "discuss.channel"

    whatsapp_gateway_id = fields.Many2one(
        "mail.gateway",
        domain=[("gateway_type", "=", "whatsapp")],
        help="Gateway de WhatsApp vinculado. Se usa para enviar respuestas de vuelta."
    )
    
    whatsapp_token = fields.Char(
        "WhatsApp Token (wa_id)",
        help="NÃºmero de WhatsApp del contacto (sin +). Se usa para enviar mensajes de vuelta."
    )

    whatsapp_customer_partner_id = fields.Many2one(
        "res.partner",
        copy=False,
        index=True,
        help="Partner cliente identificado por el numero de WhatsApp para este canal.",
    )

    whatsapp_customer_name = fields.Char(
        copy=False,
        help="Nombre fijo del cliente de WhatsApp usado en placeholders del chatbot.",
    )

    whatsapp_chatbot_script_id = fields.Many2one(
        "chatbot.script",
        help="Ãšltimo chatbot aplicado al canal para detectar cambios de configuraciÃ³n.",
    )

    whatsapp_chatbot_signature = fields.Char(
        help="Firma del chatbot (write_date de script/pasos) para invalidar estado en canales reutilizados.",
    )

    whatsapp_bot_disabled = fields.Boolean(
        default=False,
        help="When enabled, incoming customer messages do not trigger chatbot flow.",
    )

    whatsapp_last_outbound_message_id = fields.Many2one(
        "mail.message",
        readonly=True,
        copy=False,
        help="Last discuss message forwarded to WhatsApp to avoid duplicate sends.",
    )

    whatsapp_last_outbound_sent_at = fields.Datetime(
        readonly=True,
        copy=False,
        help="Timestamp of last discuss message forwarded to WhatsApp.",
    )

    whatsapp_last_outbound_fingerprint = fields.Char(
        readonly=True,
        copy=False,
        help="Last outbound fingerprint used to avoid near-duplicate sends.",
    )

    whatsapp_conversation_state = fields.Selection(
        selection=[("open", "Open"), ("closed", "Closed")],
        default="open",
        required=True,
        copy=False,
        help="Conversation state used by the bridge to decide channel reuse.",
    )

    whatsapp_closed_at = fields.Datetime(
        copy=False,
        help="When the WhatsApp conversation was marked as closed.",
    )

    whatsapp_closed_reason = fields.Selection(
        selection=[
            ("manual", "Manual"),
            ("timeout", "Inactivity Timeout"),
            ("lead_created", "Lead Created"),
            ("system", "System"),
        ],
        copy=False,
        help="Reason used when the bridge marks a conversation as closed.",
    )

    def write(self, vals):
        res = super().write(vals)
        if self.env.context.get("skip_whatsapp_state_sync"):
            return res

        if "livechat_end_dt" in vals:
            for channel in self.filtered(lambda ch: ch.whatsapp_gateway_id and ch.channel_type == "livechat"):
                if channel.livechat_end_dt and channel.whatsapp_conversation_state != "closed":
                    channel.sudo().with_context(skip_whatsapp_state_sync=True).write(
                        {
                            "whatsapp_conversation_state": "closed",
                            "whatsapp_closed_at": channel.whatsapp_closed_at or channel.livechat_end_dt,
                            "whatsapp_closed_reason": channel.whatsapp_closed_reason or "system",
                        }
                    )
                elif not channel.livechat_end_dt and channel.whatsapp_conversation_state != "open":
                    channel.sudo().with_context(skip_whatsapp_state_sync=True).write(
                        {
                            "whatsapp_conversation_state": "open",
                            "whatsapp_closed_at": False,
                            "whatsapp_closed_reason": False,
                        }
                    )

        return res

    def _mark_whatsapp_conversation_closed(self, reason="manual"):
        for channel in self.sudo().filtered(lambda ch: ch.whatsapp_gateway_id and ch.channel_type == "livechat"):
            vals = {
                "whatsapp_conversation_state": "closed",
                "whatsapp_closed_at": fields.Datetime.now(),
                "whatsapp_closed_reason": reason or "manual",
            }
            if "whatsapp_bot_disabled" in channel._fields:
                vals["whatsapp_bot_disabled"] = True
            if "chatbot_current_step_id" in channel._fields:
                vals["chatbot_current_step_id"] = False
            if "livechat_end_dt" in channel._fields and not channel.livechat_end_dt:
                vals["livechat_end_dt"] = fields.Datetime.now()

            channel.with_context(skip_whatsapp_state_sync=True).write(vals)
            if hasattr(channel, "_clear_chatbot_inactivity_reminder_state"):
                channel._clear_chatbot_inactivity_reminder_state()

    def action_close_whatsapp_conversation(self):
        self._mark_whatsapp_conversation_closed(reason="manual")
        return True

    def action_reopen_whatsapp_conversation(self):
        for channel in self.sudo().filtered(lambda ch: ch.whatsapp_gateway_id and ch.channel_type == "livechat"):
            vals = {
                "whatsapp_conversation_state": "open",
                "whatsapp_closed_at": False,
                "whatsapp_closed_reason": False,
            }
            if "whatsapp_bot_disabled" in channel._fields:
                vals["whatsapp_bot_disabled"] = False
            if "livechat_end_dt" in channel._fields:
                vals["livechat_end_dt"] = False
            channel.with_context(skip_whatsapp_state_sync=True).write(vals)
        return True

    @api.model
    def _cron_close_stale_whatsapp_conversations(self):
        now = fields.Datetime.now()
        gateways = self.env["mail.gateway"].sudo().search(
            [
                ("gateway_type", "=", "whatsapp"),
                ("route_to_livechat", "=", True),
                ("whatsapp_auto_close_after_minutes", ">", 0),
            ]
        )

        for gateway in gateways:
            cutoff = now - timedelta(minutes=gateway.whatsapp_auto_close_after_minutes)
            domain = [
                ("channel_type", "=", "livechat"),
                ("whatsapp_gateway_id", "=", gateway.id),
                ("whatsapp_conversation_state", "=", "open"),
                ("write_date", "<=", fields.Datetime.to_string(cutoff)),
            ]
            if "livechat_end_dt" in self._fields:
                domain.append(("livechat_end_dt", "=", False))
            stale_channels = self.sudo().search(domain)
            stale_channels._mark_whatsapp_conversation_closed(reason="timeout")

    @api.model
    def _cron_delete_closed_whatsapp_conversations(self):
        now = fields.Datetime.now()
        gateways = self.env["mail.gateway"].sudo().search(
            [
                ("gateway_type", "=", "whatsapp"),
                ("route_to_livechat", "=", True),
                ("whatsapp_auto_delete_closed_after_days", ">", 0),
            ]
        )

        for gateway in gateways:
            cutoff = now - timedelta(days=gateway.whatsapp_auto_delete_closed_after_days)
            closed_channels = self.sudo().search(
                [
                    ("channel_type", "=", "livechat"),
                    ("whatsapp_gateway_id", "=", gateway.id),
                    ("whatsapp_conversation_state", "=", "closed"),
                    ("whatsapp_closed_at", "!=", False),
                    ("whatsapp_closed_at", "<=", fields.Datetime.to_string(cutoff)),
                ]
            )
            closed_channels.unlink()

    def message_post(self, **kwargs):
        """
        Intercepta message_post() para canales Live Chat vinculados a WhatsApp.
        
        Si es respuesta del operador/bot en un canal Live Chat vinculado a WhatsApp, 
        envÃ­a el mensaje de vuelta hacia WhatsApp.
        
        Si es el primer mensaje del cliente (guest), dispara el bot.
        """
        self.ensure_one()

        # Para pasos de selecciÃ³n del chatbot, setear selected_answer_id
        # y normalizar el body para que en Discuss se vea el texto humano.
        channel_for_post = self._prepare_guest_chatbot_post(kwargs)
        bridge_interactive_mode = bool(self.env.context.get("bridge_interactive_mode"))
        bridge_no_gateway_notification = bool(self.env.context.get("bridge_no_gateway_notification"))

        # Para mensajes de pasos interactivos del chatbot, el bridge es
        # responsable del outbound para enviar botones/listas de WhatsApp.
        if (
            self.whatsapp_gateway_id
            and self.channel_type == "livechat"
            and bridge_interactive_mode
            and bridge_no_gateway_notification
        ):
            channel_for_post = channel_for_post.with_context(no_gateway_notification=True)

        result = super(DiscussChannel, channel_for_post).message_post(**kwargs)
        
        # Verificar si este canal estÃ¡ vinculado a WhatsApp
        if not self.whatsapp_gateway_id or self.channel_type != "livechat":
            return result
        
        # Obtener el mensaje que se acaba de crear
        message = result if isinstance(result, models.Model) else None
        if not message or message._name != "mail.message":
            return result
        
        is_customer_message = self._is_whatsapp_customer_message(message)

        if is_customer_message:
            # Es el contacto quien envía­a (guest o partner mapeado por gateway).

            if self.whatsapp_bot_disabled:
                return result

            self._ensure_livechat_bot_configuration_fresh()

            # Si aún no hay estado del bot, publicar welcome steps.
            # Si ya existe paso actual, procesar la respuesta y avanzar flujo.
            try:
                # Invalidate cached step before each access to ensure fresh reads
                # after script edits from other sessions or concurrent modifications.
                current_step = self.sudo().chatbot_current_step_id
                if current_step:
                    current_step.invalidate_recordset()
                    if hasattr(self, "_set_chatbot_inactivity_reminder_state"):
                        self._set_chatbot_inactivity_reminder_state(current_step)
                
                if current_step and self._is_chatbot_cycle_completed():
                    return result
                elif current_step:
                    self._advance_livechat_bot_from_guest_message(message)
                else:
                    self._trigger_livechat_bot_from_channel()
            except Exception as e:
                pass
            
            return result
        
        # Ensure chatbot placeholders are rendered in Discuss before any outbound
        # forwarding logic, so livechat transcript and WhatsApp payload stay aligned.
        self._render_chatbot_placeholders_on_message(message)


        if not self._should_forward_outbound_whatsapp_message(message):
            return result

        # El outbound estÃ¡ndar (texto y adjuntos) lo resuelve OCA.
        # Solo tomamos control cuando el bot publica un paso interactivo.
        if not bridge_interactive_mode:
            return result

        # Si no suprimimos OCA para este post, el envío estándar ya fue
        # gestionado por mail_gateway_whatsapp y no debemos duplicar.
        if not bridge_no_gateway_notification:
            return result

        if self._is_duplicate_outbound_whatsapp_message(message):
            return result

        gateway = self.whatsapp_gateway_id
        token = self._get_whatsapp_token()
        body_text = html2plaintext(message.body or "").strip()
        if not gateway or not token:
            return result

        if not self._send_chatbot_interactive_to_whatsapp(gateway, token, message, body_text):
            return result

        self._mark_outbound_whatsapp_message_sent(message)

        return result

    def _render_chatbot_placeholders_on_message(self, message):
        """Render chatbot placeholders on outbound bot messages before forwarding."""
        self.ensure_one()
        if not message or message._name != "mail.message":
            return
        if message.message_type != "comment":
            return

        body_text = html2plaintext(message.body or "").strip()
        if "{{" not in body_text:
            return

        step = self._get_chatbot_step_for_message(message)
        context_values = self._get_whatsapp_template_context_values()
        if step and hasattr(step, "_get_message_context_values"):
            context_values = step._get_message_context_values(self)

        render_values = {
            **context_values,
            "current_value": (
                step._get_current_partner_field_value(self)
                if step and hasattr(step, "_get_current_partner_field_value")
                else ""
            ) or "",
        }

        _logger.info(
            "[CHATBOT RENDER][LIVECHAT] BEFORE channel=%s message=%s step=%s template=%s context=%s",
            self.id,
            message.id,
            step.id if step else False,
            body_text,
            render_values,
        )

        if step and hasattr(step, "_render_prompt_template"):
            rendered = step._render_prompt_template(body_text, render_values)
        else:
            rendered = self._render_whatsapp_template(body_text, render_values)

        _logger.info(
            "[CHATBOT RENDER][LIVECHAT] AFTER channel=%s message=%s step=%s rendered=%s",
            self.id,
            message.id,
            step.id if step else False,
            rendered,
        )

        if "{{" in (rendered or ""):
            _logger.warning(
                "[CHATBOT RENDER][LIVECHAT] Unresolved placeholders channel=%s message=%s rendered=%s",
                self.id,
                message.id,
                rendered,
            )

        if not rendered or rendered == body_text:
            return

        rendered_html = plaintext2html(rendered)
        if rendered_html != (message.body or ""):
            message.sudo().write({"body": rendered_html})
            message.invalidate_recordset()

    def _should_forward_outbound_whatsapp_message(self, message):
        """Only forward operator-authored comment messages to WhatsApp."""
        if not message:
            return False
        if message.message_type != "comment":
            return False
        if not message.author_id or message.author_guest_id:
            return False
        if self._is_whatsapp_customer_message(message):
            return False
        return True

    def _is_duplicate_outbound_whatsapp_message(self, message):
        self.ensure_one()
        if not message:
            return False

        if (
            self.whatsapp_last_outbound_message_id
            and self.whatsapp_last_outbound_message_id.id == message.id
        ):
            return True

        if not self.whatsapp_last_outbound_sent_at or not self.whatsapp_last_outbound_fingerprint:
            return False

        now = fields.Datetime.now()
        if now - self.whatsapp_last_outbound_sent_at > timedelta(seconds=90):
            return False

        current_fingerprint = self._get_outbound_whatsapp_fingerprint(message)
        is_duplicate = current_fingerprint == self.whatsapp_last_outbound_fingerprint
        if is_duplicate:
            return True
        return is_duplicate

    def _mark_outbound_whatsapp_message_sent(self, message):
        self.ensure_one()
        if not message:
            return
        fingerprint = self._get_outbound_whatsapp_fingerprint(message)
        self.sudo().write(
            {
                "whatsapp_last_outbound_message_id": message.id,
                "whatsapp_last_outbound_sent_at": fields.Datetime.now(),
                "whatsapp_last_outbound_fingerprint": fingerprint,
            }
        )

    def _get_outbound_whatsapp_fingerprint(self, message):
        body_text = html2plaintext(message.body or "").strip()
        payload_key = "|".join(
            [
                str(self.id),
                str(message.author_id.id if message.author_id else 0),
                str(message.message_type or ""),
                str(message.subtype_id.id if message.subtype_id else 0),
                body_text,
                ",".join(str(attachment_id) for attachment_id in sorted(message.attachment_ids.ids)),
            ]
        )
        return hashlib.sha1(payload_key.encode("utf-8")).hexdigest()

    def _is_whatsapp_customer_message(self, message):
        """Determina si un message_post corresponde al cliente de WhatsApp."""
        if not message:
            return False

        # Caso clÃ¡sico livechat: cliente anÃ³nimo como guest.
        if message.author_guest_id and not message.author_id:
            return True

        # En canales con gateway_id enlazado, OCA puede resolver al cliente
        # como partner (author_id) vÃ­a res.partner.gateway.channel.
        if message.author_id:
            # Mensajes del operador/bot no son cliente.
            if self.livechat_operator_id and message.author_id == self.livechat_operator_id:
                return False

            # Si el partner estÃ¡ mapeado por gateway+token del canal, es cliente.
            mapping_domain = [
                ("gateway_id", "=", self.whatsapp_gateway_id.id),
                ("gateway_token", "=", str(self.whatsapp_token or "")),
                ("partner_id", "=", message.author_id.id),
            ]
            mapped_partner = self.env["res.partner.gateway.channel"].sudo().search(
                mapping_domain,
                limit=1,
            )
            if mapped_partner:
                return True

        return False

    def _prepare_guest_chatbot_post(self, kwargs):
        """Prepara el post guest para pasos de selecciÃ³n del chatbot."""
        if not self.whatsapp_gateway_id or self.channel_type != "livechat":
            return self
        author_guest_id = kwargs.get("author_guest_id")
        author_partner_id = kwargs.get("author_id")
        is_customer_partner = self._is_whatsapp_customer_partner_id(author_partner_id)
        if not author_guest_id and not is_customer_partner:
            return self

        current_step = self.sudo().chatbot_current_step_id
        if current_step:
            # Drop cached field values so the next accesses reload the step
            # from the database after script edits.
            current_step.invalidate_recordset()
            chatbot_lang = self._get_whatsapp_chatbot_lang()
            if chatbot_lang:
                current_step = current_step.with_context(lang=chatbot_lang)
        if not current_step or current_step.step_type != "question_selection":
            return self

        body = kwargs.get("body") or ""
        selected_answer = self._match_chatbot_answer_from_text(current_step, body)
        if not selected_answer:
            return self

        # Preservar trazabilidad humana en Discuss: en lugar del id tÃ©cnico
        # (ej. odoo_ans_10), guardamos el texto de la opciÃ³n elegida.
        selected_answer_name = (selected_answer.name or "").strip()
        if selected_answer_name:
            kwargs["body"] = selected_answer_name

        return self.with_context(selected_answer_id=selected_answer.id)

    def _get_whatsapp_chatbot_lang(self):
        company_lang = self.company_id.partner_id.lang if self.company_id and self.company_id.partner_id else False
        return company_lang or self.env.lang

    def _is_whatsapp_customer_partner_id(self, partner_id):
        """Indica si un partner_id corresponde al cliente WhatsApp del canal."""
        if not partner_id or not self.whatsapp_gateway_id or not self.whatsapp_token:
            return False

        mapped_partner = self.env["res.partner.gateway.channel"].sudo().search(
            [
                ("gateway_id", "=", self.whatsapp_gateway_id.id),
                ("gateway_token", "=", str(self.whatsapp_token)),
                ("partner_id", "=", partner_id),
            ],
            limit=1,
        )
        return bool(mapped_partner)

    def _match_chatbot_answer_from_text(self, step, body):
        """Mapea texto entrante de WhatsApp a una respuesta del paso actual."""
        if not step or step.step_type != "question_selection" or not step.answer_ids:
            return self.env["chatbot.script.answer"]

        plain = html2plaintext(body or "").strip()
        if not plain:
            return self.env["chatbot.script.answer"]

        normalized = plain.casefold()

        # Coincidencia exacta con el nombre completo.
        for answer in step.answer_ids:
            answer_name = (answer.name or "").strip()
            if answer_name and answer_name.casefold() == normalized:
                return answer

        # Coincidencia tolerante para tÃ­tulos truncados de WhatsApp.
        # Botones: title <= 20, listas: title <= 24.
        for answer in step.answer_ids:
            answer_name = (answer.name or "").strip()
            if not answer_name:
                continue
            if answer_name.casefold()[:20] == normalized or answer_name.casefold()[:24] == normalized:
                return answer

        match = re.fullmatch(r"odoo_ans_(\d+)", normalized)
        if match:
            answer_id = int(match.group(1))
            return step.answer_ids.filtered(lambda a: a.id == answer_id)[:1]

        # Soporta respuestas tipo "1", "2", o "1. texto".
        number_match = re.match(r"^(\d+)", normalized)
        if number_match:
            index = int(number_match.group(1)) - 1
            answers = step.answer_ids
            if 0 <= index < len(answers):
                return answers[index]

        if normalized.isdigit():
            index = int(normalized) - 1
            answers = step.answer_ids
            if 0 <= index < len(answers):
                return answers[index]

        return self.env["chatbot.script.answer"]

    def _advance_livechat_bot_from_guest_message(self, guest_message):
        """Avanza el flujo del chatbot usando la respuesta del guest."""
        current_step = self.sudo().chatbot_current_step_id
        if not current_step:
            return
        
        # Drop cached field values so the next accesses reload the step
        # from the database after script edits.
        current_step.invalidate_recordset()
        chatbot_lang = self._get_whatsapp_chatbot_lang()
        if chatbot_lang:
            current_step = current_step.with_context(lang=chatbot_lang)

        guest_raw_body = guest_message.body or ""
        guest_plain_body = html2plaintext(guest_raw_body).strip()


        step_for_process = current_step
        answer_text_for_process = guest_plain_body
        if current_step.step_type == "question_selection":
            selected_answer = self._match_chatbot_answer_from_text(current_step, guest_plain_body)
            if selected_answer:
                # _process_answer del core valida contra el texto de la opciÃ³n.
                # Si llega un id tÃ©cnico (ej. odoo_ans_4), usar el nombre completo.
                answer_text_for_process = (selected_answer.name or "").strip() or guest_plain_body
                step_for_process = current_step.with_context(selected_answer_id=selected_answer.id)
                self._persist_selected_answer_for_current_step(current_step, selected_answer)
                self._normalize_guest_message_selection_text(guest_message, selected_answer)
                selected_ids = (
                    self.sudo().chatbot_message_ids.mapped("user_script_answer_id").ids
                )

        try:
            next_step = step_for_process._process_answer(self, answer_text_for_process)
        except ValidationError as err:
            chatbot = current_step.chatbot_script_id
            if chatbot:
                self._chatbot_post_message(chatbot, plaintext2html(str(err)))
            return

        if not next_step:
            return

        if chatbot_lang:
            next_step = next_step.with_context(lang=chatbot_lang)

        if next_step.step_type == "question_selection":
            next_step.with_context(
                bridge_interactive_mode=True,
                bridge_no_gateway_notification=True,
            )._process_step(self)
        else:
            next_step._process_step(self)
        self._close_livechat_if_lead_created(next_step)

    def _close_livechat_if_lead_created(self, processed_step):
        """Cierra la sesiÃ³n livechat cuando el bot ejecuta el paso de crear lead."""
        if not processed_step or processed_step.step_type not in {"create_lead", "livechat_to_crm"}:
            return

        channel_sudo = self.sudo()
        if "livechat_end_dt" in channel_sudo._fields and channel_sudo.livechat_end_dt:
            return

        try:
            operator_name = (
                channel_sudo.livechat_operator_id.user_livechat_username
                or channel_sudo.livechat_operator_id.name
                or "Operador"
            )
            channel_sudo._close_livechat_session(operator=operator_name)
            channel_sudo._mark_whatsapp_conversation_closed(reason="lead_created")
        except Exception as e:
            pass

    def _is_chatbot_cycle_completed(self):
        """Indica si el canal quedÃ³ en paso final del bot sin esperar nueva respuesta."""
        step = self.sudo().chatbot_current_step_id
        if not step:
            return False

        if not step._is_last_step(self):
            return False

        input_step_types = {
            "question_selection",
            "question_customer_name",
            "question_email",
            "question_phone",
            "free_input_single",
            "free_input_multi",
        }
        return step.step_type not in input_step_types

    def _is_attachment_upload_allowed_for_chatbot_step(self):
        """Permite upload en livechat cuando el paso activo del bot solicita adjunto."""
        self.ensure_one()
        if self.channel_type != "livechat":
            return False
        step = self.sudo().chatbot_current_step_id
        return bool(step and step.step_type == "question_attachment")

    def _persist_selected_answer_for_current_step(self, current_step, selected_answer):
        """Asegura que la respuesta elegida quede persistida antes del cÃ¡lculo de next step."""
        if not current_step or not selected_answer:
            return

        chatbot_message = self.env["chatbot.message"].sudo().search(
            [
                ("discuss_channel_id", "=", self.id),
                ("script_step_id", "=", current_step.id),
            ],
            order="id desc",
            limit=1,
        )
        if not chatbot_message:
            return

        vals = {}
        if "user_script_answer_id" in chatbot_message._fields:
            vals["user_script_answer_id"] = selected_answer.id
        if "user_raw_script_answer_id" in chatbot_message._fields:
            vals["user_raw_script_answer_id"] = selected_answer.id
        if "user_raw_answer" in chatbot_message._fields:
            vals["user_raw_answer"] = selected_answer.name
        if vals:
            chatbot_message.write(vals)


    def _normalize_guest_message_selection_text(self, guest_message, selected_answer):
        """Reemplaza ids tÃ©cnicos por el texto de opciÃ³n para transcript/CRM."""
        if not guest_message or not selected_answer:
            return

        answer_name = (selected_answer.name or "").strip()
        if not answer_name:
            return

        raw_body = html2plaintext(guest_message.body or "").strip()
        # Evita sobreescrituras innecesarias cuando ya estÃ¡ en formato humano.
        if raw_body.casefold() == answer_name.casefold():
            return

        if not (
            re.fullmatch(r"odoo_ans_(\d+)", raw_body.casefold())
            or raw_body.isdigit()
            or raw_body.casefold().startswith("odoo_ans_")
        ):
            return

        guest_message.sudo().write({"body": plaintext2html(answer_name)})


    def _trigger_livechat_bot_from_channel(self):
        """
        Dispara el bot cuando se recibe el primer mensaje del usuario.
        
        Busca las reglas configuradas en el canal livechat y ejecuta el 
        chatbot_script asociado, publicando los welcome steps.
        """
        gateway = self.whatsapp_gateway_id
        if not gateway or not gateway.livechat_channel_id:
            return

        chatbot_script = self._get_livechat_chatbot_script()
        if not chatbot_script:
            self.sudo().write({
                "whatsapp_chatbot_script_id": False,
                "whatsapp_chatbot_signature": False,
            })
            if hasattr(self, "_clear_chatbot_inactivity_reminder_state"):
                self._clear_chatbot_inactivity_reminder_state()
            return
        
        # Obtener los welcome steps del bot
        welcome_steps = chatbot_script._get_welcome_steps()
        if not welcome_steps:
            return

        # En canales reutilizados, limpiar respuestas previas evita que
        # _fetch_next_step tome ramas con answers histÃ³ricos.
        self._reset_chatbot_answer_history()
        
        # Actualizar el channel con el current_step_id
        chatbot_signature = self._get_chatbot_signature(chatbot_script)
        self.sudo().write({
            "chatbot_current_step_id": welcome_steps[-1].id if welcome_steps else False,
            "whatsapp_chatbot_script_id": chatbot_script.id,
            "whatsapp_chatbot_signature": chatbot_signature,
            "whatsapp_bot_disabled": False,
        })
        if hasattr(self, "_clear_chatbot_inactivity_reminder_state"):
            self._clear_chatbot_inactivity_reminder_state()
        
        # Publicar los welcome steps con el idioma de la empresa si existe.
        company_lang = self.company_id.partner_id.lang
        script_for_post = chatbot_script.with_context(lang=company_lang) if company_lang else chatbot_script
        script_for_post._post_welcome_steps(self)
        

    def _reset_chatbot_answer_history(self):
        """Limpia respuestas persistidas de chatbot para reiniciar el Ã¡rbol de decisiÃ³n."""
        chatbot_messages = self.env["chatbot.message"].sudo().search(
            [("discuss_channel_id", "=", self.id)]
        )
        if not chatbot_messages:
            return

        # Compatibilidad entre versiones/parches de Odoo: no todos los campos
        # de trazas de respuesta existen en todas las instalaciones.
        fields_to_clear = {
            "user_script_answer_id": False,
            "user_raw_script_answer_id": False,
            "user_raw_answer": False,
        }
        available_fields = chatbot_messages._fields
        vals = {
            field_name: value
            for field_name, value in fields_to_clear.items()
            if field_name in available_fields
        }
        if not vals:
            return

        chatbot_messages.write(vals)

    def _get_livechat_chatbot_script(self):
        """Obtiene el chatbot.script activo asociado al canal livechat de este bridge."""
        gateway = self.whatsapp_gateway_id
        chatbot_script = self.env["chatbot.script"]
        if not gateway or not gateway.livechat_channel_id:
            return chatbot_script

        rule_model = self.env["im_livechat.channel.rule"]
        rule_domain = [
            ("channel_id", "=", gateway.livechat_channel_id.id),
            ("chatbot_script_id", "!=", False),
            ("chatbot_script_id.active", "=", True),
        ]
        if "chatbot_channel_scope" in rule_model._fields:
            rule_domain.append(("chatbot_channel_scope", "in", ["both", "whatsapp"]))

        rules = rule_model.sudo().search(
            rule_domain,
            order="sequence asc, id asc",
        )
        if not rules:
            return chatbot_script

        # Si el canal ya tiene operador bot, priorizamos la regla cuyo bot coincide.
        rule = rules.filtered(
            lambda r: r.chatbot_script_id.operator_partner_id == self.livechat_operator_id
        )[:1] or rules[:1]
        chatbot_script = rule.chatbot_script_id

        return chatbot_script

    def _get_chatbot_signature(self, chatbot_script):
        """Devuelve una firma simple del bot para detectar cambios de configuraciÃ³n."""
        if not chatbot_script:
            return False

        timestamps = [chatbot_script.write_date]
        timestamps.extend(chatbot_script.script_step_ids.mapped("write_date"))
        timestamps.extend(chatbot_script.script_step_ids.mapped("answer_ids.write_date"))
        timestamps = [ts for ts in timestamps if ts]
        if not timestamps:
            return False

        return fields.Datetime.to_string(max(timestamps))

    def _ensure_livechat_bot_configuration_fresh(self):
        """Resetea estado de bot si cambiÃ³ script o pasos desde la Ãºltima ejecuciÃ³n."""
        if not self.whatsapp_gateway_id or self.channel_type != "livechat":
            return

        configured_script = self._get_livechat_chatbot_script()
        channel_sudo = self.sudo()
        current_step = channel_sudo.chatbot_current_step_id

        if not configured_script:
            if channel_sudo.chatbot_current_step_id:
                channel_sudo.write({"chatbot_current_step_id": False})
            channel_sudo.write({
                "whatsapp_chatbot_script_id": False,
                "whatsapp_chatbot_signature": False,
            })
            return

        configured_signature = self._get_chatbot_signature(configured_script)
        has_script_changed = (
            channel_sudo.whatsapp_chatbot_script_id
            and channel_sudo.whatsapp_chatbot_script_id != configured_script
        )
        has_step_script_mismatch = (
            current_step
            and current_step.chatbot_script_id
            and current_step.chatbot_script_id != configured_script
        )
        has_signature_changed = (
            bool(channel_sudo.whatsapp_chatbot_signature)
            and bool(configured_signature)
            and channel_sudo.whatsapp_chatbot_signature != configured_signature
        )
        is_signature_tracking_missing = bool(
            current_step and configured_signature and not channel_sudo.whatsapp_chatbot_signature
        )


        if has_script_changed or has_step_script_mismatch or has_signature_changed or is_signature_tracking_missing:
            channel_sudo.write({"chatbot_current_step_id": False})
            channel_sudo.chatbot_message_ids.unlink()
            if hasattr(channel_sudo, "_clear_chatbot_inactivity_reminder_state"):
                channel_sudo._clear_chatbot_inactivity_reminder_state()

        channel_sudo.write({
            "whatsapp_chatbot_script_id": configured_script.id,
            "whatsapp_chatbot_signature": configured_signature,
        })

    def _send_chatbot_interactive_to_whatsapp(self, gateway, token, message, body_text):
        """EnvÃ­a opciones de chatbot como botones/lista interactiva de WhatsApp."""
        step = self._get_chatbot_step_for_message(message)
        if not step or step.step_type != "question_selection" or not step.answer_ids:
            return False

        answers = step.answer_ids.filtered(lambda answer: (answer.name or "").strip())
        if not answers:
            return False

        prompt = (body_text or "").strip()
        if not prompt:
            return False

        prompt = prompt[:1024]

        if len(answers) <= 3:
            buttons = []
            for answer in answers:
                button_title = answer.name.strip()[:20]
                if not button_title:
                    continue
                buttons.append({
                    "type": "reply",
                    "reply": {
                        "id": f"odoo_ans_{answer.id}",
                        "title": button_title,
                    },
                })

            if not buttons:
                return False

            payload = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": token,
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": prompt},
                    "action": {"buttons": buttons},
                },
            }
            self._post_whatsapp_payload(gateway, payload)
            return True

        rows = []
        for answer in answers[:10]:
            full_name = answer.name.strip()
            if not full_name:
                continue
            rows.append({
                "id": f"odoo_ans_{answer.id}",
                "title": full_name[:24],
                "description": full_name[:72] if len(full_name) > 24 else "",
            })

        if not rows:
            return False

        list_button = prompt[:20] or rows[0]["title"]
        section_title = prompt[:24] or rows[0]["title"]

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": token,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": prompt},
                "action": {
                    "button": list_button,
                    "sections": [{
                        "title": section_title,
                        "rows": rows,
                    }],
                },
            },
        }
        self._post_whatsapp_payload(gateway, payload)
        return True

    def _render_whatsapp_template(self, template, values):
        pattern = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")

        def _replace(match):
            return str(values.get(match.group(1), ""))

        return pattern.sub(_replace, template or "")

    def _get_whatsapp_template_context_values(self):
        customer_partner = self._get_whatsapp_customer_partner_from_channel()
        guest_name = self.whatsapp_customer_name or self._get_whatsapp_guest_name_from_channel()
        if customer_partner and customer_partner.name:
            guest_name = ""

        company_partner = customer_partner.parent_id if customer_partner else self.env["res.partner"]
        customer_name = ""
        if customer_partner and customer_partner.name:
            customer_name = customer_partner.name
        elif guest_name:
            customer_name = guest_name

        return {
            "customer_name": customer_name,
            "partner_name": customer_partner.name if customer_partner else "",
            "guest_name": guest_name,
            "customer_email": customer_partner.email if customer_partner else "",
            "customer_phone": (customer_partner.phone or customer_partner.mobile) if customer_partner else "",
            "customer_vat": customer_partner.vat if customer_partner else "",
            "delivery_address": customer_partner.street if customer_partner else "",
            "company_name": company_partner.name if company_partner else "",
            "company_vat": company_partner.vat if company_partner else "",
        }

    def _get_whatsapp_customer_partner_from_channel(self):
        self.ensure_one()
        if "whatsapp_customer_partner_id" in self._fields and self.whatsapp_customer_partner_id:
            if self._is_valid_whatsapp_customer_partner(self.whatsapp_customer_partner_id):
                return self.whatsapp_customer_partner_id

        if self.whatsapp_gateway_id and self.whatsapp_token:
            mapped_partner = self.env["res.partner.gateway.channel"].sudo().search(
                [
                    ("gateway_id", "=", self.whatsapp_gateway_id.id),
                    ("gateway_token", "=", str(self.whatsapp_token)),
                ],
                limit=1,
            ).partner_id
            # Priorizar el partner ya mapeado al token del canal.
            if mapped_partner and self._is_valid_whatsapp_customer_partner(mapped_partner):
                return mapped_partner

        return self.env["res.partner"]

    def _is_valid_whatsapp_customer_partner(self, partner):
        if not partner:
            return False
        if self.livechat_operator_id and partner == self.livechat_operator_id:
            return False
        if partner.user_ids.filtered(lambda user: not user.share):
            return False
        return True

    def _get_whatsapp_guest_name_from_channel(self):
        self.ensure_one()
        guest_members = self.channel_member_ids.filtered("guest_id")
        if guest_members and guest_members[0].guest_id.name:
            return guest_members[0].guest_id.name.strip()

        guest_messages = self.message_ids.filtered("author_guest_id")
        if guest_messages:
            guest_name = guest_messages.sorted(lambda message: message.id)[-1].author_guest_id.name
            if guest_name:
                return guest_name.strip()

        if getattr(self, "anonymous_name", False):
            return self.anonymous_name.strip()

        return ""

    def _get_chatbot_step_for_message(self, message):
        """Obtiene el step de chatbot asociado a un mail.message del canal."""
        chatbot_message = self.env["chatbot.message"].sudo().search(
            [
                ("mail_message_id", "=", message.id),
                ("discuss_channel_id", "=", self.id),
            ],
            limit=1,
        )
        return chatbot_message.script_step_id

    def _post_whatsapp_payload(self, gateway, payload):
        """EnvÃ­a un payload a WhatsApp Cloud API para mensajes interactivos."""
        try:
            response = requests.post(
                f"https://graph.facebook.com/"
                f"v{gateway.whatsapp_version}/{gateway.whatsapp_from_phone}/messages",
                headers={"Authorization": f"Bearer {gateway.token}"},
                json=payload,
                timeout=10,
            )
            response.raise_for_status()
            response.json()
        except Exception as e:
            raise

    def _get_whatsapp_token(self):
        """
        Obtiene el token (wa_id) del contacto de WhatsApp en este canal Live Chat.
        
        Intenta obtenerlo de (en orden):
        1. whatsapp_token del canal (guardado al crear el canal)
        2. Partner del canal: extrae de phone_sanitized (formato: "+wa_id")
        3. Guest del canal: extrae del nombre (formato: "contact_name (token)")
        
        Returns:
            str: Token del contacto (nÃºmero de WhatsApp sin +) o None
        """
        # OPCIÃ“N 1: Token guardado en el canal (mÃ¡s rÃ¡pido y confiable)
        if self.whatsapp_token:
            return self.whatsapp_token
        
        # OPCIÃ“N 2: Buscar partner vinculado al canal
        # Los partners estÃ¡n en channel_member_ids con partner_id
        partner_members = self.channel_member_ids.filtered("partner_id")
        
        if partner_members:
            for member in partner_members:
                partner = member.partner_id
                if partner and partner.phone_sanitized:
                    # phone_sanitized estÃ¡ en formato "+5491234567890"
                    # Extraer solo el nÃºmero sin el +
                    token = partner.phone_sanitized.lstrip("+")
                    return token
        
        # OPCIÃ“N 3: Buscar token en guest del canal
        guest_members = self.channel_member_ids.filtered("guest_id")
        
        if not guest_members:
            return None
        
        # Hay un solo guest por sesiÃ³n de Live Chat
        guest = guest_members[0].guest_id
        
        if not guest or not guest.name:
            return None
        
        # El token estÃ¡ en el nombre del guest
        # Formato: "contact_name (token)" o simplemente "token"
        guest_name = guest.name
        
        # Intentar extraer token entre parÃ©ntesis
        if "(" in guest_name and ")" in guest_name:
            try:
                token = guest_name.split("(")[1].split(")")[0]
                return token
            except (IndexError, ValueError):
                pass
        
        # Si no hay parÃ©ntesis, el nombre es el token
        token = guest_name
        
        
        return token
