"use client";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { toast } from "sonner";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { updateTenantConfig } from "@/lib/api/tenants";

const schema = z.object({
  model: z.string().min(1),
  base_url: z.string().url(),
  api_key: z.string().min(1),
});

type FormValues = z.infer<typeof schema>;

export default function LlmSettingsPage() {
  const [submitting, setSubmitting] = useState(false);
  const { register, handleSubmit, getValues, formState: { errors } } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { model: "", base_url: "", api_key: "" },
  });

  const onSubmit = async (values: FormValues) => {
    setSubmitting(true);
    try {
      await updateTenantConfig({ llm: values });
      toast.success("saved");
    } catch (e: any) {
      toast.warning(e?.message || "save failed");
    } finally {
      setSubmitting(false);
    }
  };

  const onTest = () => {
    const v = getValues();
    if (!v.model || !v.base_url || !v.api_key) {
      toast.error("fill all fields first");
      return;
    }
    toast.info("test endpoint pending backend support");
  };

  return (
    <div className="space-y-4">
      <h2 className="text-xl font-semibold">LLM</h2>
      <p className="text-sm text-muted-foreground">
        Per-tenant LLM override. Backend PATCH endpoint pending — saves
        won't persist server-side until implemented.
      </p>

      <Card>
        <CardContent className="pt-6">
          <form onSubmit={handleSubmit(onSubmit)} className="space-y-3">
            <div className="space-y-2">
              <Label htmlFor="model">Model</Label>
              <Input id="model" placeholder="gpt-4o" {...register("model")} />
              {errors.model && <p className="text-sm text-destructive">{errors.model.message}</p>}
            </div>
            <div className="space-y-2">
              <Label htmlFor="base_url">Base URL</Label>
              <Input id="base_url" placeholder="https://api.openai.com/v1" {...register("base_url")} />
              {errors.base_url && <p className="text-sm text-destructive">{errors.base_url.message}</p>}
            </div>
            <div className="space-y-2">
              <Label htmlFor="api_key">API key</Label>
              <Input id="api_key" type="password" {...register("api_key")} />
              {errors.api_key && <p className="text-sm text-destructive">{errors.api_key.message}</p>}
            </div>
            <div className="flex gap-2">
              <Button type="submit" disabled={submitting}>{submitting ? "Saving…" : "Save"}</Button>
              <Button type="button" variant="outline" onClick={onTest}>Test</Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
