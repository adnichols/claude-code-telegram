"""Message manager for handling multi-message streaming and organization."""

import asyncio
from typing import Dict, List, Optional, Any, Callable
from datetime import datetime, timedelta

import structlog
from telegram import Update, Message, InlineKeyboardMarkup
from telegram.error import TelegramError, BadRequest

from ..models.stream_context import StreamContext, StreamMessage
from .streaming import StreamProcessor

logger = structlog.get_logger()


class MessageManager:
    """Manages multiple messages for streaming Claude responses."""
    
    def __init__(self, stream_processor: StreamProcessor):
        """Initialize message manager."""
        self.stream_processor = stream_processor
        self.pending_updates: Dict[int, List[Dict]] = {}  # user_id -> list of pending updates
        self.update_tasks: Dict[int, asyncio.Task] = {}  # user_id -> update task
        
    async def start_streaming_response(self, update: Update, user_id: int, 
                                     session_id: Optional[str] = None) -> StreamContext:
        """Start a streaming response session."""
        chat_id = update.effective_chat.id
        
        try:
            context = await self.stream_processor.start_stream(
                user_id=user_id,
                chat_id=chat_id,
                update=update,
                session_id=session_id
            )
            
            # Start the update processing task for this user
            self._start_update_task(user_id)
            
            return context
            
        except Exception as e:
            logger.error("Failed to start streaming response", user_id=user_id, error=str(e))
            raise
    
    async def send_stream_message(self, user_id: int, message_type: str, 
                                content: str, parse_mode: str = "Markdown",
                                reply_markup: Optional[InlineKeyboardMarkup] = None) -> Optional[Message]:
        """Send a new message in the stream."""
        context = self.stream_processor.get_stream_context(user_id)
        if not context:
            logger.warning("No stream context found for user", user_id=user_id)
            return None
        
        try:
            # We need to get the bot instance to send messages
            # This will be injected from the handler
            bot = getattr(self, '_bot', None)
            if not bot:
                logger.error("Bot instance not available for sending messages")
                return None
            
            telegram_msg = await bot.send_message(
                chat_id=context.chat_id,
                text=content,
                parse_mode=parse_mode,
                reply_markup=reply_markup
            )
            
            # Add to stream context
            stream_msg = context.add_message(message_type, content, telegram_msg)
            
            logger.debug(
                "Sent stream message",
                user_id=user_id,
                message_type=message_type,
                message_id=telegram_msg.message_id
            )
            
            return telegram_msg
            
        except BadRequest as e:
            if "can't parse entities" in str(e).lower():
                # Fallback to plain text if markdown parsing fails
                logger.warning("Markdown parsing failed, sending as plain text", error=str(e))
                try:
                    telegram_msg = await bot.send_message(
                        chat_id=context.chat_id,
                        text=content,
                        reply_markup=reply_markup
                    )
                    stream_msg = context.add_message(message_type, content, telegram_msg)
                    return telegram_msg
                except Exception as fallback_e:
                    logger.error("Failed to send even plain text message", error=str(fallback_e))
                    return None
            else:
                logger.error("Telegram API error", user_id=user_id, error=str(e))
                return None
        except Exception as e:
            logger.error("Failed to send stream message", user_id=user_id, error=str(e))
            return None
    
    async def update_stream_message(self, user_id: int, message_id: int, 
                                  new_content: str, message_type: str,
                                  parse_mode: str = "Markdown") -> bool:
        """Update an existing stream message."""
        context = self.stream_processor.get_stream_context(user_id)
        if not context:
            return False
        
        stream_msg = context.get_message(message_type, message_id)
        if not stream_msg or not stream_msg.telegram_message:
            logger.warning(
                "Stream message not found for update",
                user_id=user_id,
                message_id=message_id,
                message_type=message_type
            )
            return False
        
        # Queue the update instead of doing it immediately to handle rate limits
        self._queue_message_update(user_id, {
            'action': 'update',
            'stream_msg': stream_msg,
            'new_content': new_content,
            'parse_mode': parse_mode
        })
        
        return True
    
    async def handle_tool_start(self, user_id: int, tool_name: str, 
                              tool_input: Optional[Dict] = None) -> None:
        """Handle the start of a tool execution."""
        escaped_tool_name = self._escape_markdown(tool_name)
        tool_text = f"🔧 *Using {escaped_tool_name}*"
        
        # Add tool input information if available
        if tool_input:
            input_info = self._format_tool_input(tool_name, tool_input)
            if input_info:
                tool_text += f": {input_info}"
        
        await self.send_stream_message(user_id, 'tool', tool_text)
        
        # Update stream context
        context = self.stream_processor.get_stream_context(user_id)
        if context:
            context.start_tool(tool_name)
    
    async def handle_tool_complete(self, user_id: int, tool_name: str, 
                                 success: bool = True, error: Optional[str] = None,
                                 execution_time_ms: Optional[int] = None) -> None:
        """Handle the completion of a tool execution."""
        context = self.stream_processor.get_stream_context(user_id)
        if not context:
            return
        
        tool_exec = context.complete_tool(tool_name, success, error)
        if not tool_exec or not tool_exec.message_id:
            return
        
        # Update the tool message
        status_emoji = "❌" if not success else "✅"
        duration_text = f" ({execution_time_ms}ms)" if execution_time_ms else ""
        escaped_tool_name = self._escape_markdown(tool_name)
        
        if not success:
            updated_text = f"{status_emoji} *{escaped_tool_name} failed*{duration_text}"
            if error:
                error_preview = error[:100] + "..." if len(error) > 100 else error
                escaped_error = self._escape_markdown(error_preview)
                updated_text += f"\n`{escaped_error}`"
        else:
            updated_text = f"{status_emoji} *{escaped_tool_name} complete*{duration_text}"
        
        await self.update_stream_message(
            user_id, tool_exec.message_id, updated_text, 'tool'
        )
    
    async def handle_content_stream(self, user_id: int, content_chunk: str) -> None:
        """Handle streaming content updates."""
        context = self.stream_processor.get_stream_context(user_id)
        if not context:
            return
        
        # Add to content buffer
        context.append_content(content_chunk)
        
        # Get or create content message
        content_msg = context.get_message('content')
        
        if content_msg:
            # Update existing content message with rate limiting
            self._queue_content_update(user_id, context)
        else:
            # Create new content message
            initial_content = f"🤖 *Claude Response:*\n\n{content_chunk}"
            await self.send_stream_message(user_id, 'content', initial_content)
    
    async def finalize_stream(self, user_id: int, cost: float = 0.0,
                            follow_up_suggestions: Optional[List[str]] = None) -> None:
        """Finalize the streaming session."""
        context = self.stream_processor.get_stream_context(user_id)
        if not context:
            return
        
        try:
            # Flush any remaining content updates
            await self._flush_pending_updates(user_id)
            
            # Send any remaining content
            remaining_content = context.flush_buffer()
            if remaining_content:
                content_msg = context.get_message('content')
                if content_msg:
                    final_content = content_msg.content + remaining_content
                    await self.update_stream_message(
                        user_id, content_msg.message_id, final_content, 'content'
                    )
            
            # Send completion message
            await self._send_completion_message(context, cost, follow_up_suggestions)
            
            # Clean up
            await self._cleanup_user_streaming(user_id)
            
        except Exception as e:
            logger.error("Failed to finalize stream", user_id=user_id, error=str(e))
    
    async def _send_completion_message(self, context: StreamContext, cost: float,
                                     follow_up_suggestions: Optional[List[str]]) -> None:
        """Send the completion status message."""
        tools_count = len(context.tools)
        duration = datetime.now() - context.start_time
        duration_text = f"{duration.total_seconds():.1f}s"
        
        status_text = f"✅ *Session Complete*"
        status_text += f"\n💰 Cost: ${cost:.4f}"
        status_text += f"\n⏱️ Duration: {duration_text}"
        
        if tools_count > 0:
            status_text += f"\n🔧 Tools used: {tools_count}"
        
        # Add follow-up suggestions if available
        reply_markup = None
        if follow_up_suggestions:
            from .formatting import ResponseFormatter
            # This would create suggestion buttons - simplified for now
            status_text += "\n\n💡 *What would you like to do next?*"
        
        await self.send_stream_message(
            context.user_id, 'status', status_text, reply_markup=reply_markup
        )
    
    def _queue_message_update(self, user_id: int, update_data: Dict) -> None:
        """Queue a message update to handle rate limiting."""
        if user_id not in self.pending_updates:
            self.pending_updates[user_id] = []
        
        self.pending_updates[user_id].append(update_data)
    
    def _queue_content_update(self, user_id: int, context: StreamContext) -> None:
        """Queue a content update with rate limiting."""
        content_msg = context.get_message('content')
        if not content_msg:
            return
        
        # Get chunk from buffer if available
        chunk = context.get_buffer_chunk()
        if chunk:
            new_content = content_msg.content + chunk
            self._queue_message_update(user_id, {
                'action': 'update_content',
                'stream_msg': content_msg,
                'new_content': new_content,
                'parse_mode': 'Markdown'
            })
    
    def _start_update_task(self, user_id: int) -> None:
        """Start the background task for processing updates."""
        if user_id in self.update_tasks:
            # Cancel existing task
            self.update_tasks[user_id].cancel()
        
        self.update_tasks[user_id] = asyncio.create_task(
            self._process_updates_loop(user_id)
        )
    
    async def _process_updates_loop(self, user_id: int) -> None:
        """Background loop to process queued updates with rate limiting."""
        try:
            while True:
                await asyncio.sleep(1)  # Update interval
                
                if user_id not in self.pending_updates:
                    continue
                
                updates = self.pending_updates[user_id]
                if not updates:
                    continue
                
                # Process one update per loop to respect rate limits
                update_data = updates.pop(0)
                
                try:
                    await self._process_single_update(update_data)
                except Exception as e:
                    logger.warning("Failed to process update", error=str(e))
                
                # Clean up empty list
                if not updates:
                    del self.pending_updates[user_id]
                    
        except asyncio.CancelledError:
            logger.debug("Update task cancelled", user_id=user_id)
        except Exception as e:
            logger.error("Update loop failed", user_id=user_id, error=str(e))
    
    async def _process_single_update(self, update_data: Dict) -> None:
        """Process a single queued update."""
        action = update_data['action']
        stream_msg = update_data['stream_msg']
        new_content = update_data['new_content']
        parse_mode = update_data.get('parse_mode', 'Markdown')
        
        if not stream_msg.telegram_message:
            return
        
        try:
            await stream_msg.telegram_message.edit_text(
                new_content,
                parse_mode=parse_mode
            )
            stream_msg.update_content(new_content)
            
        except BadRequest as e:
            if "not modified" in str(e).lower():
                # Content is the same, ignore
                pass
            elif "can't parse entities" in str(e).lower():
                # Fallback to plain text
                logger.warning("Markdown parsing failed in update, trying plain text", error=str(e))
                try:
                    await stream_msg.telegram_message.edit_text(new_content)
                    stream_msg.update_content(new_content)
                except Exception as fallback_e:
                    logger.warning("Failed to update even as plain text", error=str(fallback_e))
            else:
                logger.warning("Failed to edit message", error=str(e))
        except Exception as e:
            logger.warning("Failed to update message", error=str(e))
    
    async def _flush_pending_updates(self, user_id: int) -> None:
        """Flush all pending updates for a user."""
        updates = self.pending_updates.get(user_id, [])
        
        for update_data in updates:
            try:
                await self._process_single_update(update_data)
            except Exception as e:
                logger.warning("Failed to flush update", error=str(e))
        
        # Clear pending updates
        self.pending_updates.pop(user_id, None)
    
    async def _cleanup_user_streaming(self, user_id: int) -> None:
        """Clean up streaming resources for a user."""
        # Cancel update task
        if user_id in self.update_tasks:
            self.update_tasks[user_id].cancel()
            del self.update_tasks[user_id]
        
        # Clear pending updates
        self.pending_updates.pop(user_id, None)
        
        # Clean up stream context
        await self.stream_processor.cleanup_stream(user_id)
    
    def _format_tool_input(self, tool_name: str, tool_input: Dict) -> Optional[str]:
        """Format tool input for display."""
        if tool_name.lower() in ['read', 'write', 'edit']:
            file_path = tool_input.get('file_path') or tool_input.get('path')
            if file_path:
                return f"`{file_path}`"
        elif tool_name.lower() == 'bash':
            command = tool_input.get('command', '')
            if command:
                # Truncate long commands
                return f"`{command[:50]}...`" if len(command) > 50 else f"`{command}`"
        
        return None
    
    def set_bot_instance(self, bot: Any) -> None:
        """Set the bot instance for sending messages."""
        self._bot = bot
    
    async def handle_error(self, user_id: int, error: str) -> None:
        """Handle streaming errors by sending error message."""
        escaped_error = self._escape_markdown(error)
        error_text = f"❌ *Streaming Error*\n\n`{escaped_error}`"
        await self.send_stream_message(user_id, 'error', error_text)
        
        # Clean up the stream
        await self._cleanup_user_streaming(user_id)
    
    def _escape_markdown(self, text: str) -> str:
        """Escape special Markdown characters."""
        if not text:
            return ""
        # Escape special markdown characters
        escape_chars = ['*', '_', '`', '[', ']', '(', ')', '~', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
        for char in escape_chars:
            text = text.replace(char, f'\\{char}')
        return text