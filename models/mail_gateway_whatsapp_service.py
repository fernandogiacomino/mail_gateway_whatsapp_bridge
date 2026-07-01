from datetime import timedelta
import re
from odoo import models, fields



class MailGatewayWhatsappService(models.AbstractModel):
    """
    Extiende el servicio de WhatsApp Gateway para interceptar canales
    y redirigir a Live Chat cuando esté configurado.
    
    Puntos de extensión:
    - _get_channel(): Obtiene/crea canal (interceptación principal)
    - _get_livechat_channel(): Crea canal Live Chat para WhatsApp
    - _get_livechat_channel_vals(): Prepara valores del canal Live Chat
    """
    _inherit = "mail.gateway.whatsapp"

    # Si el cliente vuelve luego de este umbral, se considera una nueva
    # conversación y se reinicia el estado del chatbot para ese canal.
    _BOT_SESSION_TIMEOUT_MINUTES = 30

    def _is_internal_user_partner(self, partner):
        if not partner:
            return False
        return bool(partner.user_ids.filtered(lambda user: not user.share))

    def _first_mapped_value(self, records, field_name):
        values = records.mapped(field_name) if records and field_name in records._fields else []
        if not values:
            return False
        first = values[0]
        return first or False

    def _get_or_fix_gateway_partner_mapping(self, gateway, token, preferred_partner=None):
        """Retorna un único mapping gateway+token, consolidando duplicados históricos."""
        GatewayChannel = self.env["res.partner.gateway.channel"].sudo()
        token_value = str(token or "")
        if not gateway or not token_value:
            return GatewayChannel

        mappings = GatewayChannel.search(
            [
                ("gateway_id", "=", gateway.id),
                ("gateway_token", "=", token_value),
            ],
            order="id desc",
        )

        # Si ya existe un mapping del partner para este gateway, debe reutilizarse
        # para respetar el unique(partner_id, gateway_id).
        partner_mapping = GatewayChannel
        if preferred_partner:
            partner_mapping = GatewayChannel.search(
                [
                    ("gateway_id", "=", gateway.id),
                    ("partner_id", "=", preferred_partner.id),
                ],
                limit=1,
                order="id desc",
            )

        if partner_mapping:
            duplicates = mappings - partner_mapping
            if duplicates:
                duplicates.unlink()

            updates = {}
            if partner_mapping.gateway_token != token_value:
                updates["gateway_token"] = token_value
            if partner_mapping.name != gateway.name:
                updates["name"] = gateway.name
            if updates:
                partner_mapping.write(updates)

            stale_same_token = GatewayChannel.search(
                [
                    ("gateway_id", "=", gateway.id),
                    ("gateway_token", "=", token_value),
                    ("id", "!=", partner_mapping.id),
                ]
            )
            if stale_same_token:
                stale_same_token.unlink()

            return partner_mapping

        preferred = GatewayChannel
        if mappings:
            if preferred_partner:
                preferred = mappings.filtered(lambda mapping: mapping.partner_id == preferred_partner)[:1]

            if not preferred:
                preferred = mappings.filtered(
                    lambda mapping: mapping.partner_id and not self._is_internal_user_partner(mapping.partner_id)
                )[:1]

            if not preferred:
                preferred = mappings[:1]

            if preferred_partner and preferred and preferred.partner_id != preferred_partner:
                preferred.write({"partner_id": preferred_partner.id})

            duplicates = mappings - preferred
            if duplicates:
                duplicates.unlink()

            return preferred

        if preferred_partner:
            return GatewayChannel.create(
                {
                    "name": gateway.name,
                    "partner_id": preferred_partner.id,
                    "gateway_id": gateway.id,
                    "gateway_token": token_value,
                }
            )

        return GatewayChannel

    def _repair_gateway_partner_mapping_from_update(self, chat, message):
        """Sanea mapping gateway+token antes de procesar updates inbound."""
        if not chat:
            return

        gateway = self.env["mail.gateway"]
        if hasattr(chat, "mapped"):
            if "whatsapp_gateway_id" in chat._fields:
                gateway = chat.mapped("whatsapp_gateway_id")[:1]
            elif "gateway_id" in chat._fields:
                gateway = chat.mapped("gateway_id")[:1]
        else:
            gateway = getattr(chat, "whatsapp_gateway_id", False) or getattr(chat, "gateway_id", False)
        if not gateway or gateway.gateway_type != "whatsapp":
            return

        token_from_chat = False
        if hasattr(chat, "mapped"):
            token_from_chat = (
                self._first_mapped_value(chat, "whatsapp_token")
                or self._first_mapped_value(chat, "gateway_channel_token")
                or self._first_mapped_value(chat, "gateway_token")
            )
        else:
            token_from_chat = (
                getattr(chat, "whatsapp_token", False)
                or getattr(chat, "gateway_channel_token", False)
                or getattr(chat, "gateway_token", False)
            )

        token = (
            token_from_chat
            or (message.get("from") if isinstance(message, dict) else False)
        )
        if not token:
            return

        preferred_partner = self._get_partner_by_phone_with_prefix(gateway, token)
        self._get_or_fix_gateway_partner_mapping(gateway, token, preferred_partner=preferred_partner)

    def _normalize_gateway_chat_singleton(self, chat, message):
        """Devuelve un chat singleton cuando OCA devuelve mappings duplicados."""
        if not chat or not hasattr(chat, "_name"):
            return chat

        if chat._name != "res.partner.gateway.channel" or len(chat) <= 1:
            return chat

        gateway = chat.mapped("gateway_id")[:1] if "gateway_id" in chat._fields else self.env["mail.gateway"]
        token = (
            self._first_mapped_value(chat, "gateway_token")
            or (message.get("from") if isinstance(message, dict) else False)
        )
        if gateway and token:
            fixed_chat = self._get_or_fix_gateway_partner_mapping(gateway, token)
            if fixed_chat:
                return fixed_chat

        return chat[:1]

    def _process_update(self, chat, message, value):
        """Evita singleton errors cuando existen mappings duplicados por token."""
        self._repair_gateway_partner_mapping_from_update(chat, message)
        chat = self._normalize_gateway_chat_singleton(chat, message)
        try:
            return super()._process_update(chat, message, value)
        except ValueError as error:
            if "Expected singleton: res.partner" not in str(error):
                raise
            self._repair_gateway_partner_mapping_from_update(chat, message)
            chat = self._normalize_gateway_chat_singleton(chat, message)
            return super()._process_update(chat, message, value)

    def _ensure_whatsapp_gateway_runtime_config(self, gateway):
        """Normaliza configuración mínima para procesar updates de WhatsApp."""
        if not gateway or gateway.gateway_type != "whatsapp":
            return

        # En OCA/social la descarga de adjuntos usa:
        # https://graph.facebook.com/v{gateway.whatsapp_version}/{media_id}
        # Si whatsapp_version está vacío, termina en /vFalse/... y falla con 401/404.
        if not gateway.whatsapp_version:
            gateway.sudo().write({"whatsapp_version": "23.0"})

        if not gateway.token:
            return

    def _reset_stale_chatbot_state(self, channel):
        """Reinicia chatbot_current_step_id si la conversación quedó vieja."""
        if not channel or not channel.chatbot_current_step_id:
            return

        MailMessage = self.env["mail.message"].sudo()
        customer_domain = [
            ("model", "=", "discuss.channel"),
            ("res_id", "=", channel.id),
            ("author_guest_id", "!=", False),
        ]

        if channel.whatsapp_gateway_id and channel.whatsapp_token:
            mapped_partner_ids = self.env["res.partner.gateway.channel"].sudo().search(
                [
                    ("gateway_id", "=", channel.whatsapp_gateway_id.id),
                    ("gateway_token", "=", str(channel.whatsapp_token)),
                ]
            ).mapped("partner_id").ids
            if mapped_partner_ids:
                customer_domain = [
                    ("model", "=", "discuss.channel"),
                    ("res_id", "=", channel.id),
                    "|",
                    ("author_guest_id", "!=", False),
                    ("author_id", "in", mapped_partner_ids),
                ]

        last_customer_message = MailMessage.search(
            customer_domain,
            limit=1,
            order="create_date desc, id desc",
        )

        if not last_customer_message:
            # Si se limpiaron mensajes manualmente, el estado del bot puede quedar
            # apuntando a un Ã¡rbol viejo. Reiniciamos para forzar bienvenida nueva.
            previous_step = channel.chatbot_current_step_id
            reset_vals = {"chatbot_current_step_id": False}
            if "chatbot_inactivity_step_id" in channel._fields:
                reset_vals.update(
                    {
                        "chatbot_inactivity_step_id": False,
                        "chatbot_inactivity_armed_at": False,
                        "chatbot_inactivity_sent_at": False,
                    }
                )
            channel.sudo().write(reset_vals)
            channel.sudo().chatbot_message_ids.unlink()
            return

        if not last_customer_message.create_date:
            return

        session_timeout = timedelta(minutes=self._BOT_SESSION_TIMEOUT_MINUTES)
        inactivity = fields.Datetime.now() - last_customer_message.create_date
        if inactivity < session_timeout:
            return

        previous_step = channel.chatbot_current_step_id
        reset_vals = {"chatbot_current_step_id": False}
        if "chatbot_inactivity_step_id" in channel._fields:
            reset_vals.update(
                {
                    "chatbot_inactivity_step_id": False,
                    "chatbot_inactivity_armed_at": False,
                    "chatbot_inactivity_sent_at": False,
                }
            )
        channel.sudo().write(reset_vals)
        channel.sudo().chatbot_message_ids.unlink()

    def _normalize_interactive_update(self, update):
        """
        Normaliza updates interactivos de WhatsApp a formato texto.

        El parser base del gateway procesa bien mensajes `text`, pero en algunos
        escenarios puede omitir `interactive.button_reply` / `interactive.list_reply`.
        Para asegurar continuidad del chatbot, convertimos esas respuestas a
        `messages[0].text.body` usando primero el id tÃ©cnico (ej. `odoo_ans_42`).
        """
        if not isinstance(update, dict):
            return

        messages = update.get("messages") or []
        if not messages:
            return

        interactive_found = False
        for index, message in enumerate(messages):
            if not isinstance(message, dict) or message.get("type") != "interactive":
                continue

            interactive_found = True
            interactive = message.get("interactive") or {}
            parsed_reply = self._extract_interactive_reply(interactive)
            if not parsed_reply:
                continue

            normalized_body = (
                parsed_reply.get("id")
                or parsed_reply.get("title")
                or parsed_reply.get("description")
            )
            if not normalized_body:
                continue

            # Conserva trazabilidad del payload original para debug.
            message.setdefault("context", {})
            message["context"]["bridge_interactive_reply"] = parsed_reply

            message["type"] = "text"
            message["text"] = {"body": normalized_body}


        if not interactive_found:
            return

    def _extract_interactive_reply(self, interactive):
        """Extrae datos relevantes desde interactive.button_reply/list_reply."""
        if not isinstance(interactive, dict):
            return {}

        button_reply = interactive.get("button_reply")
        if isinstance(button_reply, dict):
            return {
                "kind": "button_reply",
                "id": button_reply.get("id"),
                "title": button_reply.get("title"),
            }

        list_reply = interactive.get("list_reply")
        if isinstance(list_reply, dict):
            return {
                "kind": "list_reply",
                "id": list_reply.get("id"),
                "title": list_reply.get("title"),
                "description": list_reply.get("description"),
            }

        return {}

    def _get_channel(self, gateway, token, update, force_create=False):
        """
        Obtiene o crea un canal para el mensaje de WhatsApp.
        
        Si el gateway está configurado para rutear a Live Chat,
        crea/obtiene un canal de Live Chat en lugar del canal gateway.
        
        Args:
            gateway: mail.gateway record (WhatsApp)
            token: wa_id del contacto (ej: "5491234567890")
            update: dict con estructura del webhook
            force_create: si True, crea canal si no existe
        
        Returns:
            discuss.channel record (gateway o livechat según configuración)
        """
        has_messages = bool((update or {}).get("messages"))

        self._ensure_whatsapp_gateway_runtime_config(gateway)

        # Callbacks de estado (delivered/read/ack) no requieren ruteo.
        # Dejamos que el flujo base los procese para responder 200 rápidamente.
        if not has_messages:
            return super()._get_channel(gateway, token, update, force_create)

        # Normaliza respuestas interactive para que el parser base siempre
        # tenga un `messages[0].text.body` consistente.
        self._normalize_interactive_update(update)

        # Si el gateway NO tiene routing a Live Chat, comportamiento normal
        if not gateway.route_to_livechat or not gateway.livechat_channel_id:
            return super()._get_channel(gateway, token, update, force_create)
        
        
        # Crear/obtener canal Live Chat en su lugar
        return self._get_livechat_channel(gateway, token, update, force_create)

    def _get_partner_by_phone_with_prefix(self, gateway, token):
        """
        Busca un partner por teléfono considerando prefijos ajustables de país.
    
        Intenta múltiples variaciones del número para manejar prefijos que 
        WhatsApp añade en ciertos países.
        
        Ejemplo Argentina:
        - Token WhatsApp: "5491234567890" (54=país, 9=prefijo, 1234567890=número)
        - Odoo phone_sanitized: "+5491234567890" o "+541234567890"
        - Prefijo ajustable configurado: "9"
        
        Args:
            gateway: mail.gateway record (WhatsApp)
            token: wa_id del contacto (ej: "5491234567890")
        
        Returns:
            res.partner record si se encuentra, None si no
        """
        ResPartner = self.env["res.partner"].sudo()

        # Variaciones de telefono a intentar
        token_digits = re.sub(r"\D", "", str(token or ""))
        if not token_digits:
            return None

        phone_variations = ["+" + token_digits]

        # Usar codigo de pais fijo de la compania del gateway (si existe).
        company_country_code = re.sub(
            r"\D",
            "",
            str(
                (gateway.company_id.country_id.phone_code if gateway.company_id and gateway.company_id.country_id else "")
                or (self.env.company.country_id.phone_code if self.env.company and self.env.company.country_id else "")
                or ""
            ),
        )
        prefix = re.sub(r"\D", "", (gateway.whatsapp_phone_prefix or "").strip())

        if company_country_code:
            if token_digits.startswith(company_country_code):
                local_number = token_digits[len(company_country_code):]
            else:
                local_number = token_digits

            # Variante normalizada con codigo pais fijo
            normalized_number = "+" + company_country_code + local_number
            if normalized_number not in phone_variations:
                phone_variations.append(normalized_number)

            if prefix and local_number:
                if local_number.startswith(prefix):
                    # WhatsApp trae prefijo extra: probar sin prefijo
                    no_prefix_number = "+" + company_country_code + local_number[len(prefix):]
                    if no_prefix_number not in phone_variations:
                        phone_variations.append(no_prefix_number)
                else:
                    # Odoo podria guardar numero con prefijo extra: probar con prefijo
                    with_prefix_number = "+" + company_country_code + prefix + local_number
                    if with_prefix_number not in phone_variations:
                        phone_variations.append(with_prefix_number)

        # 1) Coincidencia rapida por phone_sanitized exacto
        partner = ResPartner.search(
            [("phone_sanitized", "in", phone_variations)],
            limit=1,
        )
        if partner:
            return partner

        # 2) Fallback: comparar contra phone/mobile normalizados
        variation_digits = {re.sub(r"\D", "", val) for val in phone_variations if val}
        variation_digits.discard("")
        if not variation_digits:
            return None

        tail = token_digits[-7:] if len(token_digits) >= 7 else token_digits
        candidates = ResPartner.search(
            [
                "|",
                ("phone", "ilike", tail),
                ("mobile", "ilike", tail),
            ]
        )
        for candidate in candidates:
            candidate_digits = {
                re.sub(r"\D", "", candidate.phone or ""),
                re.sub(r"\D", "", candidate.mobile or ""),
                re.sub(r"\D", "", candidate.phone_sanitized or ""),
            }
            candidate_digits.discard("")
            if candidate_digits & variation_digits:
                return candidate

        return None

    def _get_livechat_channel(self, gateway, token, update, force_create=False):
        """
        Obtiene o crea un canal Live Chat para el contacto de WhatsApp.
        
        Flujo:
        1. Busca si existe un mapping previo (gateway_token â†’ partner)
        2. Si no existe, busca un partner por phone_sanitized (con ajuste de prefijos)
        3. Si existe partner â†’ crea/obtiene canal con el partner como autor
        4. Si NO existe partner â†’ crea/obtiene canal con un guest anÃ³nimo como autor
           (el usuario puede luego vincular/crear partner desde Discuss usando mail_guest_manage)
        
        Returns:
            discuss.channel record (livechat)
        """
        DiscussChannel = self.env["discuss.channel"].sudo()
        
        contact_name = self._get_contact_name(update)
        
        # PASO 1: Buscar mapping previo (gateway_token â†’ partner)
        gateway_partner_mapping = self._get_or_fix_gateway_partner_mapping(gateway, token)
        
        partner = None
        if gateway_partner_mapping:
            partner = gateway_partner_mapping.partner_id
        
        # PASO 2: Buscar partner por phone_sanitized (con ajuste de prefijos de paí­s)
        if not partner:
            partner = self._get_partner_by_phone_with_prefix(gateway, token)

        if partner:
            self._get_or_fix_gateway_partner_mapping(gateway, token, preferred_partner=partner)
        
        # PASO 3: Buscar o crear canal Live Chat
        search_domain = [
            ("channel_type", "=", "livechat"),
            ("livechat_channel_id", "=", gateway.livechat_channel_id.id),
            ("whatsapp_token", "=", str(token)),
        ]
        if "whatsapp_conversation_state" in DiscussChannel._fields:
            search_domain.append(("whatsapp_conversation_state", "=", "open"))
        if "livechat_end_dt" in DiscussChannel._fields:
            search_domain.append(("livechat_end_dt", "=", False))
        
        channel = DiscussChannel.search(search_domain, limit=1)
        
        if channel:
            self._ensure_livechat_channel_gateway_binding(channel, gateway, token)
            self._sync_livechat_channel_customer_partner(channel, partner, contact_name=contact_name)
            self._reset_stale_chatbot_state(channel)
            return channel
        
        if not force_create:
            return None
        
        # Crear nuevo canal Live Chat
        channel_vals = self._get_livechat_channel_vals(
            gateway, token, contact_name, partner
        )
        channel = DiscussChannel.create(channel_vals)
        self._ensure_livechat_channel_gateway_binding(channel, gateway, token)
        
        # NOTA: NO disparar el bot aquÃ­. Se dispararÃ¡ despuÃ©s de procesar
        # el primer mensaje del cliente, para que el saludo aparezca como
        # respuesta natural, no como iniciador de la sesiÃ³n (odoobot).
        # Ver discuss_channel.py _trigger_livechat_bot_from_channel()
        
        return channel

    def _sync_livechat_channel_customer_partner(self, channel, partner, contact_name=None):
        """Persist customer identity in channel without inferring from all members."""
        if not channel:
            return

        updates = {}
        if "whatsapp_customer_name" in channel._fields:
            updates["whatsapp_customer_name"] = partner.name if partner and partner.name else (contact_name or "")
        if "whatsapp_customer_partner_id" in channel._fields:
            updates["whatsapp_customer_partner_id"] = partner.id if partner else False

        target_name = partner.name if partner and partner.name else (contact_name or "")
        if target_name and channel.name != target_name:
            updates["name"] = target_name

        if updates:
            channel.sudo().write(updates)

        if not partner:
            return

        if "channel_member_ids" not in channel._fields:
            return

        existing_partner_ids = set(channel.channel_member_ids.filtered("partner_id").mapped("partner_id").ids)
        if partner.id in existing_partner_ids:
            return

        from odoo import Command

        channel.sudo().write(
            {
                "channel_member_ids": [
                    Command.create({
                        "partner_id": partner.id,
                    })
                ]
            }
        )

    def _get_livechat_channel_vals(self, gateway, token, contact_name, partner=None):
        """
        Prepara los valores para crear un canal Live Chat.
        
        Incluye los miembros del canal directamente en los valores de creaciÃ³n.
        
        Args:
            gateway: mail.gateway record
            token: wa_id del contacto
            contact_name: nombre del contacto
            partner: res.partner record (opcional, si fue encontrado)
        
        Returns:
            dict con valores para crear discuss.channel
        """
        from odoo import Command
        
        # Preferimos el operador del chatbot del canal para arrancar la sesiÃ³n.
        # Si no hay bot configurado, usamos el fallback de operador humano.
        operator_id = self._get_livechat_chatbot_operator(gateway) or self._get_livechat_operator(gateway)
        
        # Nombre del canal: usar solo el nombre del contacto/partner.
        if partner:
            channel_name = partner.name
        else:
            channel_name = contact_name
        
        channel_vals = {
            "name": channel_name,
            "channel_type": "livechat",
            "livechat_channel_id": gateway.livechat_channel_id.id,
            "livechat_operator_id": operator_id,  # REQUERIDO para canales livechat
            "whatsapp_gateway_id": gateway.id,
            "whatsapp_token": str(token),
        }

        if "whatsapp_customer_name" in self.env["discuss.channel"]._fields:
            channel_vals["whatsapp_customer_name"] = channel_name
        if "whatsapp_customer_partner_id" in self.env["discuss.channel"]._fields:
            channel_vals["whatsapp_customer_partner_id"] = partner.id if partner else False

        # Compatibilidad con OCA mail_gateway_whatsapp: _process_update()
        # usa chat.gateway_id para descargar adjuntos inbound.
        discuss_fields = self.env["discuss.channel"]._fields
        if "gateway_id" in discuss_fields:
            channel_vals["gateway_id"] = gateway.id
        if "gateway_channel_token" in discuss_fields:
            channel_vals["gateway_channel_token"] = str(token)
        
        # Preparar miembros del canal
        members = []
        
        # Agregar operador inicial como miembro.
        # Si hay chatbot, este operador es el partner del bot.
        if operator_id:
            members.append(Command.create({
                "partner_id": operator_id,
            }))
        
        # Agregar partner o guest como miembro principal del canal
        if partner:
            members.append(Command.create({
                "partner_id": partner.id,
            }))
        else:
            # Crear guest y agregarlo como miembro
            guest = self.env["mail.guest"].sudo().create({
                "name": contact_name,
                "gateway_id": gateway.id,
                "gateway_token": str(token),
            })
            members.append(Command.create({
                "guest_id": guest.id,
            }))
        
        channel_vals["channel_member_ids"] = members
        
        return channel_vals

    def _ensure_livechat_channel_gateway_binding(self, channel, gateway, token):
        """Asegura que el canal livechat tenga gateway_id/token para OCA inbound media."""
        if not channel or not gateway:
            return

        updates = {}
        if "gateway_id" in channel._fields and not channel.gateway_id:
            updates["gateway_id"] = gateway.id
        if "gateway_channel_token" in channel._fields and not channel.gateway_channel_token:
            updates["gateway_channel_token"] = str(token)
        if "whatsapp_gateway_id" in channel._fields and not channel.whatsapp_gateway_id:
            updates["whatsapp_gateway_id"] = gateway.id
        if "whatsapp_token" in channel._fields and not channel.whatsapp_token:
            updates["whatsapp_token"] = str(token)

        if updates:
            channel.sudo().write(updates)

    def _get_livechat_chatbot_operator(self, gateway):
        """Obtiene el partner del bot asociado al canal livechat (si existe)."""
        chatbot = self._get_livechat_chatbot_script(gateway)
        bot_partner = chatbot.operator_partner_id
        if not bot_partner:
            return None

        return bot_partner.id

    def _get_livechat_chatbot_script(self, gateway):
        """Obtiene el chatbot.script activo asociado al canal livechat (si existe)."""
        chatbot_script = self.env["chatbot.script"]
        if not gateway.livechat_channel_id:
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

        rule = rules[:1]
        chatbot_script = rule.chatbot_script_id
        return chatbot_script

    def _get_livechat_operator(self, gateway):
        """
        Obtiene el ID del partner que será operador del canal Live Chat.
        
        Intenta en este orden:
        1. Primer usuario del canal livechat configurado
        2. Primer usuario administrador en el sistema
        3. Usuario actual
        
        Returns:
            int: partner_id del operador o None
        """
        User = self.env["res.users"]
        
        # Opción 1: Operadores configurados en el canal livechat
        if gateway.livechat_channel_id.user_ids:
            first_operator = gateway.livechat_channel_id.user_ids[0]
            return first_operator.partner_id.id
        
        # Opción 2: Buscar primer admin en el sistema
        admin_users = User.search(
            [("groups_id", "in", self.env.ref("base.group_system").id)],
            limit=1
        )
        if admin_users:
            return admin_users[0].partner_id.id
        
        # Opción 3: Usuario actual
        if self.env.user:
            return self.env.user.partner_id.id
        
        return None

    def _get_contact_name(self, update):
        """
        Extrae el nombre del contacto del update del webhook.
        
        Args:
            update: dict con estructura {"contacts": [...]}
        
        Returns:
            str con el nombre del contacto o "Unknown"
        """
        try:
            contacts = update.get("contacts", [])
            if contacts and len(contacts) > 0:
                profile = contacts[0].get("profile", {})
                name = profile.get("name")
                if name:
                    return name
        except (KeyError, IndexError, TypeError):
            pass
        
        return "Unknown"
