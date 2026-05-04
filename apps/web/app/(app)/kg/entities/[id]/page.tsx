import { EntityDetail } from "@/components/kg/entity-detail";

export default async function KgEntityPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <EntityDetail id={decodeURIComponent(id)} />;
}
