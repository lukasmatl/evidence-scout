export async function GET() {
  return Response.json([
    {
      id: 'usr_001',
      email: 'admin@example.test',
    },
  ]);
}
