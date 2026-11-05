import { createHash } from "node:crypto";
import type { SupportRequest } from "@egap/contracts";

export type SyntheticSupportFixture = "renewal-dispute" | "invalid-ticket-revision";
export const SYNTHETIC_TICKET = Object.freeze({ schema_version: 1, ticket_id: "CS-1042", ticket_revision: 3 } as const);

/**
 * submitFixture → 本函数：只发送预先载入实验环境的工单引用，不发送期望政策/答案。
 * 账单、时间、合同和使用记录由后续 SUT fixture loader 建立；目前尚无该服务。
 */
export function syntheticSupportFixture(kind: SyntheticSupportFixture): {
  body: string; sha256: string; expectedTicket: SupportRequest;
} {
  if (kind !== "renewal-dispute" && kind !== "invalid-ticket-revision") throw new TypeError("Unknown support fixture.");
  const body = JSON.stringify({ ...SYNTHETIC_TICKET, ticket_revision: kind === "renewal-dispute" ? 3 : 0 });
  return { body, sha256: createHash("sha256").update(body).digest("hex"), expectedTicket: SYNTHETIC_TICKET };
}
