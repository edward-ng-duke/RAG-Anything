export interface UserInfo {
  user_id: string;
  email: string;
  display_name: string | null;
}

export interface TenantBrief {
  tenant_id: string;
  display_name: string;
  role: string;
}

export interface AuthTokens {
  access_token: string;
  refresh_token: string;
  user: UserInfo;
  tenants: TenantBrief[];
}

export interface MeResponse {
  user: UserInfo;
  tenants: TenantBrief[];
}

export interface DocumentBrief {
  document_id: string;
  tenant_id: string;
  file_name: string;
  file_size: number | null;
  content_hash: string;
  mime_type: string | null;
  status: string;
  uploaded_at: string;
  indexed_at: string | null;
  error_message: string | null;
}

export interface DocumentListResponse {
  items: DocumentBrief[];
  next_cursor: string | null;
}

export interface JobResponse {
  job_id: string;
  document_id: string | null;
  job_type: string;
  status: string;
  progress: Record<string, unknown>;
  error_message: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  retries: number;
}

export interface ConversationBrief {
  conversation_id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
}

export interface MessageResponse {
  message_id: string;
  role: string;
  content: string;
  sources: Record<string, unknown> | null;
  created_at: string;
}

export interface ConversationDetailResponse {
  conversation: ConversationBrief;
  messages: MessageResponse[];
}

export interface KGEntity {
  id: string;
  entity_name: string | null;
  entity_type: string | null;
  content: string | null;
  file_path: string | null;
}

export interface KGStats {
  entities: number;
  relations: number;
  chunks: number;
}

export interface ApiError {
  message: string;
  status: number;
}
