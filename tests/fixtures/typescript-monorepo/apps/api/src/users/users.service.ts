import { Injectable } from '@nestjs/common';

export interface UserDto {
  id: string;
  email: string;
}

@Injectable()
export class UsersService {
  findAll(): UserDto[] {
    return [
      {
        id: 'usr_001',
        email: 'admin@example.test',
      },
    ];
  }

  findOne(id: string): UserDto {
    return {
      id,
      email: `${id}@example.test`,
    };
  }
}
