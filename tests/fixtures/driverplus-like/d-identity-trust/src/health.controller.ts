@Controller('health')
export class HealthController {
  @Get('live') live() { return {status: 'ok'}; }
}
