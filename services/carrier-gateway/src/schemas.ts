// Wire contracts. These mirror shipex.agent.carrier (Python) field for field; the
// Python client validates every response against its own pydantic models.
import { z } from "zod";

const isoDateTime = z.iso.datetime({ offset: true });

export const TrackingInfo = z.object({
  shipment_id: z.string().min(1),
  status: z.string().min(1),
  location: z.string().nullable().optional(),
  eta: isoDateTime.nullable().optional(),
  history: z.array(z.string()).default([]),
  missing_customs_documents: z.array(z.string()).default([]),
});

export const RebookingOption = z.object({
  option_id: z.string().min(1),
  service: z.string().min(1),
  new_eta: isoDateTime,
  cost_delta_gbp: z.number(),
});

export const CarrierFixture = z.object({
  tracking: TrackingInfo,
  rebooking_options: z.array(RebookingOption).default([]),
});

export const RebookRequest = z.strictObject({
  shipment_id: z.string().min(1),
  option_id: z.string().min(1),
});

export const CustomsDocumentRequest = z.strictObject({
  shipment_id: z.string().min(1),
  documents: z.array(z.string().min(1)).min(1).max(5),
});

export const ClaimRequest = z.strictObject({
  shipment_id: z.string().min(1),
  amount_gbp: z.number().positive(),
  reason: z.string().max(500).optional(),
});

export type TrackingInfo = z.infer<typeof TrackingInfo>;
export type RebookingOption = z.infer<typeof RebookingOption>;
export type CarrierFixture = z.infer<typeof CarrierFixture>;
