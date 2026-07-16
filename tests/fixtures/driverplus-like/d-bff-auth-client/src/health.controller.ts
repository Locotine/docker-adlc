@Controller('health')
export class HealthController {
  @Get('live') live() { return {status: 'ok'}; }
  @Get('ready') ready() { return {status: 'ok'}; }
}
