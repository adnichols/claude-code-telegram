"""Stream context data structures for real-time message streaming."""

from dataclasses import dataclass, field
from typing import Dict, List, Optional
from datetime import datetime
from telegram import Message


@dataclass
class StreamMessage:
    """Represents a message in the stream."""
    
    message_id: int
    message_type: str  # 'header', 'content', 'tool', 'status'
    content: str
    telegram_message: Optional[Message] = None
    created_at: datetime = field(default_factory=datetime.now)
    last_updated: datetime = field(default_factory=datetime.now)
    is_final: bool = False
    
    def update_content(self, new_content: str) -> None:
        """Update message content and timestamp."""
        self.content = new_content
        self.last_updated = datetime.now()


@dataclass
class ToolExecution:
    """Tracks tool execution status."""
    
    tool_name: str
    status: str  # 'started', 'in_progress', 'completed', 'error'
    start_time: datetime = field(default_factory=datetime.now)
    end_time: Optional[datetime] = None
    execution_time_ms: Optional[int] = None
    error_message: Optional[str] = None
    message_id: Optional[int] = None
    
    @property
    def duration_ms(self) -> Optional[int]:
        """Calculate duration in milliseconds."""
        if self.end_time and self.start_time:
            return int((self.end_time - self.start_time).total_seconds() * 1000)
        return None
    
    def complete(self, success: bool = True, error: Optional[str] = None) -> None:
        """Mark tool execution as complete."""
        self.end_time = datetime.now()
        self.execution_time_ms = self.duration_ms
        self.status = 'completed' if success else 'error'
        if error:
            self.error_message = error


@dataclass
class StreamContext:
    """Manages the context for a streaming Claude session."""
    
    user_id: int
    chat_id: int
    session_id: Optional[str] = None
    messages: Dict[str, StreamMessage] = field(default_factory=dict)
    tools: Dict[str, ToolExecution] = field(default_factory=dict)
    content_buffer: str = ""
    total_cost: float = 0.0
    start_time: datetime = field(default_factory=datetime.now)
    is_active: bool = True
    
    # Message tracking
    header_message_id: Optional[int] = None
    content_message_id: Optional[int] = None
    status_message_id: Optional[int] = None
    
    # Streaming settings
    chunk_size: int = 500
    update_interval_ms: int = 1000
    max_messages: int = 10
    
    def add_message(self, message_type: str, content: str, 
                   telegram_message: Optional[Message] = None) -> StreamMessage:
        """Add a new stream message."""
        message_id = len(self.messages) + 1
        stream_msg = StreamMessage(
            message_id=message_id,
            message_type=message_type,
            content=content,
            telegram_message=telegram_message
        )
        self.messages[f"{message_type}_{message_id}"] = stream_msg
        
        # Track specific message types
        if message_type == 'header':
            self.header_message_id = message_id
        elif message_type == 'content':
            self.content_message_id = message_id
        elif message_type == 'status':
            self.status_message_id = message_id
            
        return stream_msg
    
    def get_message(self, message_type: str, message_id: Optional[int] = None) -> Optional[StreamMessage]:
        """Get a message by type and optional ID."""
        if message_id:
            return self.messages.get(f"{message_type}_{message_id}")
        
        # Return the latest message of this type
        for key, msg in reversed(self.messages.items()):
            if msg.message_type == message_type:
                return msg
        return None
    
    def start_tool(self, tool_name: str) -> ToolExecution:
        """Start tracking a tool execution."""
        tool_exec = ToolExecution(tool_name=tool_name, status='started')
        self.tools[tool_name] = tool_exec
        return tool_exec
    
    def complete_tool(self, tool_name: str, success: bool = True, 
                     error: Optional[str] = None) -> Optional[ToolExecution]:
        """Complete a tool execution."""
        if tool_name in self.tools:
            self.tools[tool_name].complete(success, error)
            return self.tools[tool_name]
        return None
    
    def append_content(self, new_content: str) -> None:
        """Append content to the buffer."""
        self.content_buffer += new_content
    
    def get_buffer_chunk(self) -> Optional[str]:
        """Get a chunk of content from the buffer."""
        if len(self.content_buffer) >= self.chunk_size:
            chunk = self.content_buffer[:self.chunk_size]
            self.content_buffer = self.content_buffer[self.chunk_size:]
            return chunk
        return None
    
    def flush_buffer(self) -> str:
        """Get all remaining content from buffer."""
        content = self.content_buffer
        self.content_buffer = ""
        return content
    
    def get_session_summary(self) -> Dict:
        """Get a summary of the streaming session."""
        duration = datetime.now() - self.start_time
        return {
            'session_id': self.session_id,
            'user_id': self.user_id,
            'duration_seconds': duration.total_seconds(),
            'total_messages': len(self.messages),
            'tools_used': len(self.tools),
            'total_cost': self.total_cost,
            'is_active': self.is_active
        }
    
    def cleanup(self) -> None:
        """Mark session as inactive and prepare for cleanup."""
        self.is_active = False
        
        # Mark all messages as final
        for msg in self.messages.values():
            msg.is_final = True