"""Core streaming utilities for real-time Claude response streaming."""

import asyncio
import re
import time
from typing import Optional, Callable, Any, Dict, List
from datetime import datetime, timedelta

import structlog
from telegram import Update, Message
from telegram.error import TelegramError, BadRequest

from ..models.stream_context import StreamContext, StreamMessage, ToolExecution

logger = structlog.get_logger()


class StreamingError(Exception):
    """Exception raised during streaming operations."""
    pass


class StreamProcessor:
    """Processes and manages real-time streaming of Claude responses."""
    
    def __init__(self, settings: Any):
        """Initialize stream processor with configuration."""
        self.settings = settings
        self.active_streams: Dict[int, StreamContext] = {}
        self.rate_limiter = StreamRateLimiter()
        
        # Configuration
        self.enable_streaming = getattr(settings, 'enable_streaming_messages', True)
        self.chunk_size = getattr(settings, 'stream_chunk_size', 500)
        self.update_interval = getattr(settings, 'stream_update_interval', 1000)
        self.max_messages = getattr(settings, 'max_stream_messages', 10)
    
    async def start_stream(self, user_id: int, chat_id: int, 
                          update: Update, session_id: Optional[str] = None) -> StreamContext:
        """Start a new streaming session."""
        if not self.enable_streaming:
            raise StreamingError("Streaming is disabled")
        
        # Clean up any existing stream for this user
        await self.cleanup_stream(user_id)
        
        # Create new stream context
        context = StreamContext(
            user_id=user_id,
            chat_id=chat_id,
            session_id=session_id,
            chunk_size=self.chunk_size,
            update_interval_ms=self.update_interval,
            max_messages=self.max_messages
        )
        
        self.active_streams[user_id] = context
        
        # Send initial header message
        await self._send_header_message(context, update)
        
        logger.info(
            "Started streaming session",
            user_id=user_id,
            session_id=session_id,
            chat_id=chat_id
        )
        
        return context
    
    async def _send_header_message(self, context: StreamContext, update: Update) -> None:
        """Send the initial header message."""
        if context.session_id:
            session_info = f"Session: {self._escape_markdown(context.session_id[:8])}..."
        else:
            session_info = "New session"
        header_text = f"🚀 *Claude Session Started* • {session_info}"
        
        try:
            header_msg = await update.message.reply_text(
                header_text,
                parse_mode="Markdown",
                reply_to_message_id=update.message.message_id
            )
            
            context.add_message('header', header_text, header_msg)
            
        except Exception as e:
            logger.warning("Failed to send header message", error=str(e))
            raise StreamingError(f"Failed to initialize stream: {e}")
    
    async def handle_stream_update(self, user_id: int, update_obj: Any) -> None:
        """Handle incoming stream updates from Claude."""
        context = self.active_streams.get(user_id)
        if not context or not context.is_active:
            return
        
        try:
            if update_obj.type == "tool_use":
                await self._handle_tool_start(context, update_obj)
            elif update_obj.type == "tool_result":
                await self._handle_tool_complete(context, update_obj)
            elif update_obj.type == "assistant" and update_obj.content:
                await self._handle_content_update(context, update_obj.content)
            elif update_obj.type == "progress":
                await self._handle_progress_update(context, update_obj)
                
        except Exception as e:
            logger.warning(
                "Failed to handle stream update",
                user_id=user_id,
                update_type=getattr(update_obj, 'type', 'unknown'),
                error=str(e)
            )
    
    async def _handle_tool_start(self, context: StreamContext, update_obj: Any) -> None:
        """Handle tool execution start."""
        tool_name = getattr(update_obj, 'name', 'Unknown Tool')
        tool_exec = context.start_tool(tool_name)
        
        # Send tool start message
        tool_text = f"🔧 **Using {tool_name}**"
        
        # Add input info if available
        if hasattr(update_obj, 'input') and update_obj.input:
            input_info = self._format_tool_input(tool_name, update_obj.input)
            if input_info:
                tool_text += f": {input_info}"
        
        try:
            tool_msg = await self._send_new_message(context, tool_text, 'tool')
            tool_exec.message_id = tool_msg.message_id if tool_msg else None
            
        except Exception as e:
            logger.warning("Failed to send tool start message", error=str(e))
    
    async def _handle_tool_complete(self, context: StreamContext, update_obj: Any) -> None:
        """Handle tool execution completion."""
        tool_name = getattr(update_obj, 'tool_name', 'Unknown')
        is_error = getattr(update_obj, 'is_error', False)
        error_msg = getattr(update_obj, 'error', None) if is_error else None
        
        tool_exec = context.complete_tool(tool_name, not is_error, error_msg)
        
        if tool_exec and tool_exec.message_id:
            # Update the existing tool message
            status_emoji = "❌" if is_error else "✅"
            duration_text = f" ({tool_exec.duration_ms}ms)" if tool_exec.duration_ms else ""
            
            if is_error:
                updated_text = f"{status_emoji} **{tool_name} failed**{duration_text}"
                if error_msg:
                    updated_text += f"\n_{error_msg[:100]}..._" if len(error_msg) > 100 else f"\n_{error_msg}_"
            else:
                updated_text = f"{status_emoji} **{tool_name} complete**{duration_text}"
            
            try:
                await self._update_message(context, tool_exec.message_id, updated_text, 'tool')
            except Exception as e:
                logger.warning("Failed to update tool completion message", error=str(e))
    
    async def _handle_content_update(self, context: StreamContext, content: str) -> None:
        """Handle content streaming updates."""
        context.append_content(content)
        
        # Check if we should send a content update
        if self.rate_limiter.should_update_content(context.user_id):
            chunk = context.get_buffer_chunk()
            if chunk:
                await self._update_content_message(context, chunk)
    
    async def _handle_progress_update(self, context: StreamContext, update_obj: Any) -> None:
        """Handle progress updates (typing indicators, etc.)."""
        # For now, we'll just log progress updates
        # In the future, we could show these as temporary status updates
        progress_text = getattr(update_obj, 'content', 'Working...')
        logger.debug("Progress update", user_id=context.user_id, progress=progress_text)
    
    async def _update_content_message(self, context: StreamContext, chunk: str) -> None:
        """Update or create the main content message."""
        content_msg = context.get_message('content')
        
        if content_msg:
            # Update existing content message
            new_content = content_msg.content + chunk
            try:
                await self._update_message(context, content_msg.message_id, new_content, 'content')
                content_msg.update_content(new_content)
            except Exception as e:
                logger.warning("Failed to update content message", error=str(e))
        else:
            # Create new content message
            content_text = f"🤖 **Claude Response:**\n\n{chunk}"
            try:
                content_msg = await self._send_new_message(context, content_text, 'content')
            except Exception as e:
                logger.warning("Failed to create content message", error=str(e))
    
    async def _send_new_message(self, context: StreamContext, text: str, 
                               message_type: str) -> Optional[StreamMessage]:
        """Send a new message in the stream."""
        try:
            # This is a placeholder - the actual message sending is handled
            # by the MessageManager which has access to the bot instance
            stream_msg = context.add_message(message_type, text)
            return stream_msg
            
        except Exception as e:
            logger.error("Failed to send new stream message", error=str(e))
            return None
    
    async def _update_message(self, context: StreamContext, message_id: int, 
                             new_text: str, message_type: str) -> None:
        """Update an existing message."""
        stream_msg = context.get_message(message_type, message_id)
        if not stream_msg or not stream_msg.telegram_message:
            return
        
        try:
            await stream_msg.telegram_message.edit_text(
                new_text,
                parse_mode="Markdown"
            )
            stream_msg.update_content(new_text)
            
        except BadRequest as e:
            if "not modified" in str(e).lower():
                # Message content is the same, ignore
                pass
            else:
                logger.warning("Failed to edit message", error=str(e))
        except Exception as e:
            logger.warning("Failed to update message", error=str(e))
    
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
    
    async def finalize_stream(self, user_id: int, cost: float = 0.0, 
                             follow_up_suggestions: Optional[List[str]] = None) -> None:
        """Finalize the streaming session."""
        context = self.active_streams.get(user_id)
        if not context:
            return
        
        try:
            # Flush any remaining content
            remaining_content = context.flush_buffer()
            if remaining_content:
                await self._update_content_message(context, remaining_content)
            
            # Send completion status
            await self._send_completion_message(context, cost, follow_up_suggestions)
            
            # Mark as complete
            context.cleanup()
            context.total_cost = cost
            
            logger.info(
                "Finalized streaming session",
                user_id=user_id,
                session_summary=context.get_session_summary()
            )
            
        except Exception as e:
            logger.error("Failed to finalize stream", user_id=user_id, error=str(e))
    
    async def _send_completion_message(self, context: StreamContext, cost: float,
                                     follow_up_suggestions: Optional[List[str]]) -> None:
        """Send the final completion status message."""
        tools_count = len(context.tools)
        duration = datetime.now() - context.start_time
        duration_text = f"{duration.total_seconds():.1f}s"
        
        status_text = f"✅ **Session Complete** • Cost: ${cost:.4f} • Duration: {duration_text}"
        
        if tools_count > 0:
            status_text += f" • {tools_count} tools used"
        
        if follow_up_suggestions:
            status_text += "\n\n💡 **What would you like to do next?**"
        
        try:
            await self._send_new_message(context, status_text, 'status')
        except Exception as e:
            logger.warning("Failed to send completion message", error=str(e))
    
    async def cleanup_stream(self, user_id: int) -> None:
        """Clean up a streaming session."""
        if user_id in self.active_streams:
            context = self.active_streams[user_id]
            context.cleanup()
            del self.active_streams[user_id]
            
            logger.debug("Cleaned up streaming session", user_id=user_id)
    
    def get_stream_context(self, user_id: int) -> Optional[StreamContext]:
        """Get the current stream context for a user."""
        return self.active_streams.get(user_id)
    
    def _escape_markdown(self, text: str) -> str:
        """Escape special Markdown characters."""
        if not text:
            return ""
        # Escape special markdown characters
        escape_chars = ['*', '_', '`', '[', ']', '(', ')', '~', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
        for char in escape_chars:
            text = text.replace(char, f'\\{char}')
        return text


class StreamRateLimiter:
    """Handles rate limiting for stream updates."""
    
    def __init__(self):
        """Initialize rate limiter."""
        self.last_content_update: Dict[int, datetime] = {}
        self.min_content_interval = timedelta(seconds=1)  # Max 1 content update per second
    
    def should_update_content(self, user_id: int) -> bool:
        """Check if content should be updated based on rate limits."""
        now = datetime.now()
        last_update = self.last_content_update.get(user_id)
        
        if not last_update or (now - last_update) >= self.min_content_interval:
            self.last_content_update[user_id] = now
            return True
        
        return False
    
    def cleanup_user(self, user_id: int) -> None:
        """Clean up rate limiting data for a user."""
        self.last_content_update.pop(user_id, None)